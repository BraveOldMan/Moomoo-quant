# -*- coding: utf-8 -*-
"""新股 IPO 量化交易策略入口（单线程事件消费，消除并发下单竞态）。

运行前置条件：
  1. 启动 moomoo OpenD 网关（默认 127.0.0.1:11111）
  2. 安装依赖：pip install moomoo-api

架构：行情推送线程与主轮询线程都只向事件队列投递 (code, price)，
由唯一的消费线程串行处理决策与下单，因此策略状态无并发写入风险。

环境变量（均可选，有默认值）：
  OPEND_HOST / OPEND_PORT          OpenD 地址与端口
  TRADE_ENV                        SIMULATE（默认）或 REAL
  TRADE_PASSWORD                   实盘解锁密码（REAL 必填）
  IPO_DAYS_WINDOW                  关注上市后 N 天内新股，默认 10
  POSITION_RATIO / MAX_POSITIONS / ENTRY_TRANCHES   仓位参数
  USE_ATR_SIZING / ATR_RISK_PER_TRADE_PCT           ATR 仓位
  STOP_LOSS_PCT / TRAILING_STOP_PCT                 止损
  MIN_HOLD_DAYS                    PDT 最小持仓交易日，默认 1（0 关闭）
  DAILY_LOSS_LIMIT_PCT / CIRCUIT_BREAKER_BASELINE   组合熔断
  USE_LIMIT_ORDERS / LIMIT_PRICE_TOLERANCE_PCT      限价执行
  USE_RS / USE_ORB / USE_VWAP_SIGNAL / USE_BROKER_SIGNAL   因子开关
  ALERT_EMAIL / TELEGRAM_TOKEN / TELEGRAM_CHAT_ID   告警
"""

import logging
import queue
import threading
import time
from datetime import date, datetime, time as _time, timedelta
from zoneinfo import ZoneInfo

import moomoo as ft

from .alerts import AlertManager
from .config import Signal, StrategyConfig
from .data_access import DataAccess
from .market_calendar import get_nyse_holidays
from .monitor import RealtimeMonitor
from .persistence import PositionRecord, PositionStore
from .signals import SignalCalculator
from .strategy import IPOStrategy
from .trader import Trader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_IPO_REFRESH_INTERVAL = 300  # 每 5 分钟刷新一次 IPO 列表
_SHUTDOWN = object()  # 队列哨兵


def _is_market_open(cfg: StrategyConfig) -> bool:
    tz = ZoneInfo(cfg.market_timezone)
    now = datetime.now(tz)
    today = now.date()
    if today.weekday() >= 5:
        return False
    if today in get_nyse_holidays(today.year):
        return False
    open_h, open_m = map(int, cfg.market_open.split(":"))
    close_h, close_m = map(int, cfg.market_close.split(":"))
    t = now.time().replace(second=0, microsecond=0)
    return _time(open_h, open_m) <= t < _time(close_h, close_m)


def _is_in_open_cooldown(cfg: StrategyConfig) -> bool:
    tz = ZoneInfo(cfg.market_timezone)
    now = datetime.now(tz)
    open_h, open_m = map(int, cfg.market_open.split(":"))
    market_open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    elapsed = (now - market_open_dt).total_seconds() / 60.0
    return 0.0 <= elapsed < cfg.open_cooldown_minutes


def _fetch_recent_ipos(data: DataAccess, markets: tuple, days: int) -> dict[str, date]:
    cutoff = date.today() - timedelta(days=days)
    result: dict[str, date] = {}
    for market in markets:
        ret, df = data._quote.get_ipo_list(market)  # IPO 列表低频，无需缓存
        if ret != ft.RET_OK or df.empty:
            if ret != ft.RET_OK:
                logger.warning("get_ipo_list 失败 market=%s: %s", market, df)
            continue
        date_col = next(
            (
                c
                for c in df.columns
                if "listing" in c.lower() or "ipo_date" in c.lower()
            ),
            None,
        )
        code_col = next((c for c in df.columns if c in ("code", "stock_code")), None)
        if date_col is None or code_col is None:
            logger.warning("IPO 列表列名未识别 cols=%s", df.columns.tolist())
            continue
        for _, row in df.iterrows():
            try:
                listing_date = date.fromisoformat(str(row[date_col])[:10])
                if listing_date >= cutoff:
                    result[str(row[code_col])] = listing_date
            except (ValueError, TypeError):
                continue
    logger.info("发现近 %d 天内新股 %d 只: %s", days, len(result), list(result.keys()))
    return result


def _get_snapshot(data: DataAccess, code: str) -> tuple[float | None, int]:
    ret, df = data.get_market_snapshot(code)
    if ret != ft.RET_OK or df.empty:
        return None, 1
    row = df.iloc[0]
    try:
        price = float(row["last_price"])
    except (KeyError, TypeError, ValueError):
        price = None
    try:
        lot_size = int(row["lot_size"]) or 1
    except (KeyError, TypeError, ValueError):
        lot_size = 1
    return price, lot_size


def run() -> None:
    cfg = StrategyConfig.from_env()
    if cfg.trd_env == "REAL" and not cfg.trade_password:
        raise RuntimeError("实盘模式必须设置 TRADE_PASSWORD 环境变量")

    quote_ctx = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
    trade_ctx = ft.OpenSecTradeContext(
        filter_trdmarket=ft.TrdMarket.NONE, host=cfg.host, port=cfg.port
    )

    if cfg.trd_env == "REAL":
        ret, msg = trade_ctx.unlock_trade(cfg.trade_password)
        if ret != ft.RET_OK:
            raise RuntimeError(f"交易解锁失败: {msg}")
        logger.info("交易已解锁（实盘模式）")
    else:
        logger.info("运行在模拟交易模式")

    data = DataAccess(quote_ctx, trade_ctx, cfg)
    calculator = SignalCalculator(data, cfg)
    strategy = IPOStrategy(calculator, cfg)
    trader = Trader(trade_ctx, data, cfg)
    store = PositionStore(cfg.db_path)
    alerts = AlertManager(cfg)

    # 恢复持仓
    saved = store.load_all()
    if saved:
        logger.info("从数据库恢复 %d 只持仓", len(saved))
        for code, rec in saved.items():
            strategy.restore_position(
                code=code,
                avg_cost=rec.cost_price,
                qty=rec.qty,
                buy_date=rec.buy_date,
                tranches_bought=rec.tranches_bought,
                peak_price=rec.peak_price,
            )

    events: queue.Queue = queue.Queue()
    baseline_set_date: dict[str, date] = {}

    def process_quote(code: str, price: float) -> None:
        """单线程消费：评估并执行。仅此函数会改写策略状态与下单。"""
        if not _is_market_open(cfg):
            return

        # 每个交易日开盘后首次：注入熔断基准净值
        today = date.today()
        if baseline_set_date.get("d") != today:
            pv = trader.get_portfolio_value()
            if pv > 0:
                strategy.set_daily_baseline(pv)
                baseline_set_date["d"] = today

        decision = strategy.evaluate(code, current_price=price)
        logger.info("决策: %s", decision)

        if decision.signal == Signal.BUY:
            if _is_in_open_cooldown(cfg):
                logger.info("开盘冷静期内，跳过买入 %s", code)
                return
            _, lot_size = _get_snapshot(data, code)
            is_new = not strategy.has_position(code)
            ok, fill_price, filled = trader.buy(
                code, price, lot_size, atr=decision.atr, is_new_position=is_new
            )
            if ok:
                strategy.record_buy(code, fill_price, filled)
                store.save(
                    PositionRecord(
                        code=code,
                        cost_price=strategy.get_avg_cost(code),
                        buy_date=date.today(),
                        tranches_bought=strategy.get_tranches_bought(code),
                        peak_price=strategy.get_peak_price(code),
                        qty=strategy.get_qty(code),
                    )
                )
                alerts.send(
                    "买入成功",
                    f"{code} 成交均价={fill_price:.3f} qty={filled}"
                    f" 第{strategy.get_tranches_bought(code)}/{cfg.entry_tranches}批",
                )
            else:
                alerts.send("买入失败", f"{code} price={price:.3f} 下单未成交")

        elif decision.signal == Signal.SELL:
            if trader.sell(code, price):
                strategy.clear_position(code)
                store.delete(code)
                alerts.send("卖出", f"{code} price={price:.3f} 原因: {decision.reason}")
            else:
                alerts.send("卖出失败", f"{code} price={price:.3f} 下单未成交")

    def consumer() -> None:
        while True:
            item = events.get()
            if item is _SHUTDOWN:
                events.task_done()
                return
            code, price = item
            try:
                process_quote(code, price)
            except Exception as exc:
                logger.exception("处理事件异常 %s: %s", code, exc)
            finally:
                events.task_done()

    consumer_thread = threading.Thread(
        target=consumer, name="event-consumer", daemon=True
    )
    consumer_thread.start()

    # 行情推送只投递事件，不直接下单
    monitor = RealtimeMonitor(quote_ctx, lambda c, p: events.put((c, p)))
    monitor.start()
    if saved:
        monitor.subscribe(list(saved.keys()))

    try:
        while True:
            pv = trader.get_portfolio_value()
            if pv > 0:
                if strategy.check_and_update_circuit_breaker(pv):
                    alerts.send(
                        "组合熔断",
                        f"当日亏损超过 {cfg.daily_loss_limit_pct * 100:.0f}%，暂停买入直到次日",
                    )

            ipo_map = _fetch_recent_ipos(data, cfg.markets, cfg.ipo_days_window)
            if ipo_map:
                calculator.set_listing_dates(ipo_map)
                monitor.subscribe(list(ipo_map.keys()))

            all_codes = set(ipo_map.keys()) | strategy.get_active_codes()
            for code in all_codes:
                price, _ = _get_snapshot(data, code)
                if price is not None:
                    events.put((code, price))

            time.sleep(_IPO_REFRESH_INTERVAL)

    except KeyboardInterrupt:
        logger.info("策略已手动停止")
    finally:
        events.put(_SHUTDOWN)
        consumer_thread.join(timeout=5)
        monitor.stop()
        quote_ctx.close()
        trade_ctx.close()
        logger.info("连接已关闭")


if __name__ == "__main__":
    run()
