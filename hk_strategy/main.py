# -*- coding: utf-8 -*-
"""港股多因子量化交易策略入口（单线程事件消费，消除并发下单竞态）。

市场：港股（HKEX）。时区 Asia/Hong_Kong（无夏令时），交易时段含午休
（09:30–12:00 / 13:00–16:00）；港股无 PDT 规则，MIN_HOLD_DAYS 默认 0。
交易日历优先用 request_trading_days API 刷新，失败回退硬编码假日表。

运行前置条件：
  1. 启动 moomoo OpenD 网关（默认 127.0.0.1:11111）
  2. 安装依赖：pip install moomoo-api

架构：行情推送线程与主轮询线程都只向事件队列投递 (code, price)，
由唯一的消费线程串行处理决策与下单，因此策略状态无并发写入风险。

环境变量（均可选，有默认值）：
  OPEND_HOST / OPEND_PORT          OpenD 地址与端口
  TRADE_ENV                        SIMULATE（默认）或 REAL
  ALLOW_REAL_TRADING               实盘二次确认开关，REAL 模式须设为 yes 否则拒绝启动
  TRADE_PASSWORD                   实盘解锁密码（REAL 必填）
  IPO_DAYS_WINDOW                  关注上市后 N 天内新股，默认 10
  WATCHLIST / WATCHLIST_FILE       自选港股清单（覆盖 watchlist.txt）
  TRADE_EXCLUDED_SYMBOLS           仅观察/基准标的，不进入下单 universe
  MIN_DAILY_TURNOVER               流动性过滤阈值（HKD），默认 5,000,000
  POSITION_RATIO / MAX_POSITIONS / ENTRY_TRANCHES   仓位参数
  USE_ATR_SIZING / ATR_RISK_PER_TRADE_PCT           ATR 仓位
  STOP_LOSS_PCT / TRAILING_STOP_PCT                 止损
  MIN_HOLD_DAYS                    最小持仓交易日，默认 0（港股无 PDT）
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

import moomoo as ft

from dark_pool_proxy import DarkPoolProxyConfig, DarkPoolProxyTracker
from order_book_l2 import L2ImbalanceConfig, L2ImbalanceTracker, OrderBookCache

from .alerts import AlertManager
from .clock import market_date, market_datetime
from .config import Signal, StrategyConfig
from .data_access import DataAccess
from .market_calendar import get_hkex_holidays, refresh_trading_days_from_api
from .monitor import RealtimeMonitor
from .persistence import (
    PortfolioValueStore,
    PositionRecord,
    PositionStore,
    SignalLogStore,
)
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


def _hhmm(s: str) -> _time:
    """'HH:MM' -> datetime.time。"""
    h, m = map(int, s.split(":"))
    return _time(h, m)


def _is_market_open(cfg: StrategyConfig, now: datetime | None = None) -> bool:
    """按配置市场时区判断常规交易时段（港股双时段，午休视为闭市）。"""
    now = market_datetime(cfg.market_timezone, now=now)
    today = now.date()
    if today.weekday() >= 5:
        return False
    if today in get_hkex_holidays(today.year):
        return False
    t = now.time().replace(second=0, microsecond=0)
    morning = _hhmm(cfg.market_open) <= t < _hhmm(cfg.morning_close)
    afternoon = _hhmm(cfg.afternoon_open) <= t < _hhmm(cfg.market_close)
    return morning or afternoon


def _is_in_open_cooldown(cfg: StrategyConfig, now: datetime | None = None) -> bool:
    """按市场时区判断是否仍处于开盘冷静期。"""
    now = market_datetime(cfg.market_timezone, now=now)
    open_h, open_m = map(int, cfg.market_open.split(":"))
    market_open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    elapsed = (now - market_open_dt).total_seconds() / 60.0
    return 0.0 <= elapsed < cfg.open_cooldown_minutes


def _tradable_watchlist(cfg: StrategyConfig) -> tuple[str, ...]:
    """返回自选清单中允许进入交易决策的标的，排除指数/基准等观察项。"""

    excluded = set(cfg.trade_excluded_symbols)
    return tuple(code for code in cfg.watchlist if code not in excluded)


def _fetch_recent_ipos(
    data: DataAccess,
    markets: tuple,
    days: int,
    today: date | None = None,
) -> dict[str, date]:
    """获取近 N 个自然日内已经上市的新股，日期按市场日传入。"""
    today = today or market_date(StrategyConfig.market_timezone)
    cutoff = today - timedelta(days=days)
    result: dict[str, date] = {}
    for market in markets:
        # 显式用 ft.Market 枚举（文档约定）；字符串无对应枚举时回退原值
        mkt = getattr(ft.Market, market, market)
        ret, df = data._quote.get_ipo_list(mkt)  # IPO 列表低频，无需缓存
        if ret != ft.RET_OK or df.empty:
            if ret != ft.RET_OK:
                logger.warning("get_ipo_list 失败 market=%s: %s", market, df)
            continue
        # moomoo get_ipo_list 的上市日列名为 list_time（港股为预计上市日）；
        # 保留 listing/ipo_date 模糊匹配作兜底，防 SDK 版本差异。
        date_col = next(
            (
                c
                for c in df.columns
                if c == "list_time" or "listing" in c.lower() or "ipo_date" in c.lower()
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
                # 仅纳入近 N 天内"已上市"的新股；排除尚未上市的预计 IPO
                if cutoff <= listing_date <= today:
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
    if cfg.trd_env == "REAL":
        if not cfg.allow_real_trading:
            raise RuntimeError(
                "拒绝启动实盘：TRADE_ENV=REAL 时必须显式设置 ALLOW_REAL_TRADING=yes 作为二次确认。"
                "如需模拟交易请不要设置 TRADE_ENV（默认 SIMULATE）。"
            )
        if not cfg.trade_password:
            raise RuntimeError("实盘模式必须设置 TRADE_PASSWORD 环境变量")

    quote_ctx = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
    trade_ctx = ft.OpenSecTradeContext(
        filter_trdmarket=ft.TrdMarket.NONE, host=cfg.host, port=cfg.port
    )

    # 用 API 刷新 HKEX 交易日历（覆盖今明两年）；失败则静默回退硬编码表。
    _yr = market_date(cfg.market_timezone).year
    n_days = refresh_trading_days_from_api(
        quote_ctx, f"{_yr}-01-01", f"{_yr + 1}-12-31"
    )
    logger.info(
        "HKEX 交易日历: API 刷新 %d 天%s", n_days, "" if n_days else "（回退硬编码）"
    )

    if cfg.trd_env == "REAL":
        ret, msg = trade_ctx.unlock_trade(cfg.trade_password)
        if ret != ft.RET_OK:
            raise RuntimeError(f"交易解锁失败: {msg}")
        logger.info("交易已解锁（实盘模式）")
    else:
        logger.info("运行在模拟交易模式")

    use_order_book = (
        cfg.use_order_book_imbalance
        or cfg.use_order_book_pressure
        or cfg.use_order_book_metrics
        or cfg.use_l2_imbalance_tracker
    )
    order_book_cache = (
        OrderBookCache(cfg.order_book_cache_max_age_s) if use_order_book else None
    )
    data = DataAccess(quote_ctx, trade_ctx, cfg, order_book_cache=order_book_cache)
    signal_log = SignalLogStore(cfg.db_path)
    calculator = SignalCalculator(data, cfg, signal_log=signal_log)
    strategy = IPOStrategy(calculator, cfg)
    trader = Trader(trade_ctx, data, cfg)
    store = PositionStore(cfg.db_path)
    portfolio_values = PortfolioValueStore(cfg.db_path)
    alerts = AlertManager(cfg)
    dark_pool_tracker = None
    if cfg.use_dark_pool_proxy:
        dark_pool_tracker = DarkPoolProxyTracker(
            DarkPoolProxyConfig(
                us_min_notional=cfg.dark_pool_us_min_notional,
                hk_min_notional=cfg.dark_pool_hk_min_notional,
                alert_cooldown_s=cfg.dark_pool_alert_cooldown_s,
            )
        )
    l2_tracker = None
    if cfg.use_l2_imbalance_tracker:
        l2_tracker = L2ImbalanceTracker(
            L2ImbalanceConfig(
                level=cfg.l2_imbalance_level,
                warn=cfg.l2_imbalance_warn,
                danger=cfg.l2_imbalance_danger,
                persist_snapshots=cfg.l2_imbalance_persist_snapshots,
                alert_cooldown_s=cfg.l2_imbalance_alert_cooldown_s,
                spread_warning_bps=cfg.order_book_spread_warning_bps,
                spread_danger_bps=cfg.order_book_spread_danger_bps,
                slippage_warning_bps=cfg.order_book_slippage_warning_bps,
                slippage_danger_bps=cfg.order_book_slippage_danger_bps,
            )
        )

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

    def set_circuit_breaker_baseline(today: date, portfolio_value: float) -> None:
        """Inject the configured daily circuit-breaker baseline once per day."""
        if baseline_set_date.get("d") == today:
            return
        mode = cfg.circuit_breaker_baseline
        if mode == "prev_close":
            previous = portfolio_values.latest_before(today)
            if previous is not None and previous.value > 0:
                strategy.set_daily_baseline(previous.value)
                logger.info(
                    "组合熔断基准使用前一观测日净值: date=%s value=%.2f",
                    previous.trade_date.isoformat(),
                    previous.value,
                )
            else:
                logger.warning("缺少前一观测日净值，prev_close 将降级为 first_seen")
        elif mode == "day_open":
            strategy.set_daily_baseline(portfolio_value)
            logger.info("组合熔断基准使用当日首个开盘净值: %.2f", portfolio_value)
        elif mode != "first_seen":
            logger.warning("未知组合熔断基准 %s，将降级为 first_seen", mode)
        baseline_set_date["d"] = today

    def process_quote(code: str, price: float) -> None:
        """单线程消费：评估并执行。仅此函数会改写策略状态与下单。"""
        if not _is_market_open(cfg):
            return

        # 每个交易日开盘后首次按配置注入熔断基准净值。
        today = market_date(cfg.market_timezone)
        pv = trader.get_portfolio_value()
        if pv > 0:
            set_circuit_breaker_baseline(today, pv)
            portfolio_values.save(today, pv)

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
                        buy_date=market_date(cfg.market_timezone),
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
    # 微观结构因子（CVD/OBI）启用时追加实时订阅类型
    extra_subs = []
    if cfg.use_order_flow or cfg.use_dark_pool_proxy:
        extra_subs.append(ft.SubType.TICKER)
    if use_order_book:
        extra_subs.append(ft.SubType.ORDER_BOOK)
    if cfg.use_broker_signal:
        extra_subs.append(ft.SubType.BROKER)
    monitor = RealtimeMonitor(
        quote_ctx,
        lambda c, p: events.put((c, p)),
        extra_sub_types=extra_subs,
        order_book_cache=order_book_cache,
        l2_imbalance_tracker=l2_tracker,
        l2_alert_callback=alerts.send if l2_tracker is not None else None,
        dark_pool_proxy_tracker=dark_pool_tracker,
        dark_pool_market_date_provider=lambda: market_date(
            cfg.market_timezone
        ).isoformat(),
        dark_pool_alert_callback=alerts.send if dark_pool_tracker is not None else None,
    )
    monitor.start()
    if saved:
        monitor.subscribe(list(saved.keys()))
    # 自选 universe（非 IPO 的通用港股）：启动即订阅，全程纳入分析
    tradable_watchlist = _tradable_watchlist(cfg)
    excluded_watchlist = tuple(
        code for code in cfg.watchlist if code in set(cfg.trade_excluded_symbols)
    )
    if tradable_watchlist:
        monitor.subscribe(list(tradable_watchlist))
        logger.info(
            "自选交易 universe %d 只: %s",
            len(tradable_watchlist),
            list(tradable_watchlist),
        )
    if excluded_watchlist:
        logger.info(
            "仅观察/基准标的已排除出下单 universe: %s", list(excluded_watchlist)
        )

    try:
        while True:
            pv = trader.get_portfolio_value()
            if pv > 0:
                today = market_date(cfg.market_timezone)
                set_circuit_breaker_baseline(today, pv)
                portfolio_values.save(today, pv)
                if strategy.check_and_update_circuit_breaker(pv):
                    alerts.send(
                        "组合熔断",
                        f"当日亏损超过 {cfg.daily_loss_limit_pct * 100:.0f}%，暂停买入直到次日",
                    )

            ipo_map = _fetch_recent_ipos(
                data,
                cfg.markets,
                cfg.ipo_days_window,
                today=market_date(cfg.market_timezone),
            )
            if ipo_map:
                calculator.set_listing_dates(ipo_map)
                monitor.subscribe(list(ipo_map.keys()))

            # universe = IPO 扫描 ∪ 自选清单 ∪ 现有持仓
            all_codes = (
                set(ipo_map.keys())
                | set(tradable_watchlist)
                | strategy.get_active_codes()
            )
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
