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
  ALLOW_REAL_TRADING               实盘二次确认开关，REAL 模式须设为 yes 否则拒绝启动
  TRADE_PASSWORD                   实盘解锁密码（REAL 必填）
  IPO_DAYS_WINDOW                  关注上市后 N 天内新股，默认 10
  POSITION_RATIO / MAX_POSITIONS / ENTRY_TRANCHES   仓位参数
  USE_ATR_SIZING / ATR_RISK_PER_TRADE_PCT           ATR 仓位
  STOP_LOSS_PCT / TRAILING_STOP_PCT                 止损
  MIN_HOLD_DAYS                    PDT 最小持仓交易日，默认 1（0 关闭）
  DAILY_LOSS_LIMIT_PCT / CIRCUIT_BREAKER_BASELINE   组合熔断
  USE_LIMIT_ORDERS / LIMIT_PRICE_TOLERANCE_PCT      限价执行
  TRADE_FAILURE_ALERT_COOLDOWN_S                    买卖失败提醒冷却秒数
  USE_RS / USE_ORB / USE_VWAP_SIGNAL / USE_BROKER_SIGNAL   因子开关
  ALERT_EMAIL / TELEGRAM_TOKEN / TELEGRAM_CHAT_ID   告警
"""

import logging
import math
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
from .market_calendar import get_nyse_holidays
from .monitor import RealtimeMonitor
from .persistence import PositionRecord, PositionStore, SignalLogStore
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
_DISPLAY_NAME_FIELDS = ("name", "stock_name", "security_name", "short_name")
_EMPTY_DISPLAY_NAMES = {"", "nan", "none", "n/a", "na", "--"}
_BUY_FAILURE_ALERT_SUPPRESS_MARKERS = ("资金不足", "预算不足")


def _is_market_open(cfg: StrategyConfig, now: datetime | None = None) -> bool:
    """按配置市场时区判断常规交易时段。"""
    now = market_datetime(cfg.market_timezone, now=now)
    today = now.date()
    if today.weekday() >= 5:
        return False
    if today in get_nyse_holidays(today.year):
        return False
    open_h, open_m = map(int, cfg.market_open.split(":"))
    close_h, close_m = map(int, cfg.market_close.split(":"))
    t = now.time().replace(second=0, microsecond=0)
    return _time(open_h, open_m) <= t < _time(close_h, close_m)


def _is_in_open_cooldown(cfg: StrategyConfig, now: datetime | None = None) -> bool:
    """按市场时区判断是否仍处于开盘冷静期。"""
    now = market_datetime(cfg.market_timezone, now=now)
    open_h, open_m = map(int, cfg.market_open.split(":"))
    market_open_dt = now.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    elapsed = (now - market_open_dt).total_seconds() / 60.0
    return 0.0 <= elapsed < cfg.open_cooldown_minutes


class _FailureAlertGate:
    """按事件和标的压制重复失败提醒，不影响信号记录。"""

    def __init__(self, cooldown_s: float) -> None:
        self._cooldown_s = max(0.0, cooldown_s)
        self._last_sent: dict[tuple[str, str], float] = {}

    def should_send(
        self,
        event: str,
        code: str,
        now: float | None = None,
    ) -> bool:
        """返回当前失败事件是否应发送提醒。时间口径为 monotonic 秒。"""
        if self._cooldown_s <= 0:
            return True
        current = time.monotonic() if now is None else now
        key = (event, code)
        last = self._last_sent.get(key)
        if last is not None and current - last < self._cooldown_s:
            return False
        self._last_sent[key] = current
        return True


def _should_ignore_unheld_sell(
    has_strategy_position: bool,
    broker_qty: int,
) -> bool:
    """无本地持仓且券商仓位为 0 时，卖出信号只记录不执行。"""
    return not has_strategy_position and broker_qty <= 0


def _should_suppress_buy_failure_alert(reason: str) -> bool:
    """资金/预算不足属于预期阻断，只写日志，不反复打扰飞书群。"""
    return any(marker in reason for marker in _BUY_FAILURE_ALERT_SUPPRESS_MARKERS)


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
        # moomoo get_ipo_list 的上市日列名为 list_time（美股为预计上市日）；
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


def _snapshot_display_name(row: object, code: str) -> str:
    """从 moomoo 快照行提取提醒展示名，取不到名称时回退为代码。"""
    getter = getattr(row, "get", None)
    if not callable(getter):
        return code
    for field in _DISPLAY_NAME_FIELDS:
        raw = getter(field, None)
        if raw is None:
            continue
        name = str(raw).strip()
        if name.lower() not in _EMPTY_DISPLAY_NAMES and name != code:
            return f"{name}（{code}）"
    return code


def _get_symbol_display(data: DataAccess, code: str) -> str:
    """从行情快照获取提醒展示名，失败时保持原始代码。"""
    ret, df = data.get_market_snapshot(code)
    if ret != ft.RET_OK or df.empty:
        return code
    return _snapshot_display_name(df.iloc[0], code)


def _planned_buy_text(lot_size: int, cfg: StrategyConfig) -> str:
    """返回买入计划数量说明。"""
    lot = max(1, int(lot_size or 1))
    if cfg.order_lots_per_trade > 0:
        qty = lot * cfg.order_lots_per_trade
        return f"{cfg.order_lots_per_trade}手，qty={qty}，lot_size={lot}"
    return f"按仓位算法计算，lot_size={lot}"


def _alert_with_account_snapshot(message: str, account_snapshot: str = "") -> str:
    """把账户快照追加到飞书信号正文底部。"""
    if not account_snapshot:
        return message
    return f"{message}\n\n{account_snapshot}"


def _buy_alert_message(
    display: str,
    signal_price: float,
    lot_size: int,
    cfg: StrategyConfig,
    decision_reason: str,
    result: str,
    detail: str,
    account_snapshot: str = "",
) -> str:
    """构造买入飞书提醒正文。"""
    message = (
        f"标的：{display}\n"
        f"信号：BUY\n"
        f"信号理由：{decision_reason}\n"
        f"信号价：{signal_price:.3f}\n"
        f"计划下单：{_planned_buy_text(lot_size, cfg)}\n"
        f"执行结果：{result}\n"
        f"{detail}"
    )
    return _alert_with_account_snapshot(message, account_snapshot)


def _sell_alert_message(
    display: str,
    signal_price: float,
    decision_reason: str,
    result: str,
    detail: str,
    account_snapshot: str = "",
) -> str:
    """构造卖出飞书提醒正文。"""
    message = (
        f"标的：{display}\n"
        f"信号：SELL\n"
        f"信号理由：{decision_reason}\n"
        f"触发价：{signal_price:.3f}\n"
        "计划下单：清仓当前持仓\n"
        f"执行结果：{result}\n"
        f"{detail}"
    )
    return _alert_with_account_snapshot(message, account_snapshot)


def _safe_number(value: object) -> float | None:
    """把 moomoo 返回值转成有限浮点数，N/A、空值和非数字返回 None。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _row_number(row: object, fields: tuple[str, ...]) -> float | None:
    """按字段顺序读取首个有效数值。"""
    getter = getattr(row, "get", None)
    if not callable(getter):
        return None
    for field in fields:
        value = _safe_number(getter(field, None))
        if value is not None:
            return value
    return None


def _sum_frame_number(frame: object, fields: tuple[str, ...]) -> float | None:
    """聚合 DataFrame 中首个可用字段的数值列。"""
    empty = getattr(frame, "empty", True)
    if empty:
        return None
    columns = getattr(frame, "columns", ())
    for field in fields:
        if field not in columns:
            continue
        total = 0.0
        found = False
        for value in frame[field]:
            number = _safe_number(value)
            if number is None:
                continue
            total += number
            found = True
        if found:
            return total
    return None


def _format_money(value: float | None) -> str:
    """格式化普通金额。"""
    if value is None:
        return "--"
    return f"{value:,.2f}"


def _format_signed_money(value: float | None) -> str:
    """格式化带正负号的盈亏金额。"""
    if value is None:
        return "--"
    return f"{value:+,.2f}"


def _format_signed_pct(value: float | None) -> str:
    """格式化带正负号的百分比。"""
    if value is None:
        return "--"
    return f"{value:+.2f}%"


def _account_snapshot_text(data: DataAccess) -> str:
    """生成飞书信号底部账户快照，金额口径为 moomoo US 模拟账户 USD。"""
    ret, acc = data.accinfo_query()
    if ret != ft.RET_OK or acc.empty:
        logger.warning("账户快照查询失败: %s", acc)
        return ""

    row = acc.iloc[0]
    net_assets = _row_number(row, ("net_assets", "total_assets", "net_cash_value"))
    market_value = _row_number(row, ("market_val", "securities_assets"))
    buying_power = _row_number(row, ("power",))
    maintenance_margin = _row_number(row, ("maintenance_margin",))
    cash = _row_number(row, ("cash", "us_cash", "available_funds"))
    remaining_liquidity = (
        net_assets - maintenance_margin
        if net_assets is not None and maintenance_margin is not None
        else None
    )

    position_ret, positions = data.position_list_query()
    if position_ret == ft.RET_OK:
        today_pl = _sum_frame_number(positions, ("today_pl_val", "td_pl_val"))
        holding_pl = _sum_frame_number(positions, ("pl_val", "unrealized_pl"))
    else:
        logger.warning("账户快照持仓盈亏查询失败: %s", positions)
        today_pl = None
        holding_pl = None

    today_pl_pct = (
        today_pl / net_assets * 100.0
        if today_pl is not None and net_assets not in (None, 0.0)
        else None
    )

    items = (
        f"资产净值 {_format_money(net_assets)} 美元",
        f"今日盈亏 {_format_signed_money(today_pl)}",
        f"今日盈亏比例 {_format_signed_pct(today_pl_pct)}",
        f"持仓盈亏 {_format_signed_money(holding_pl)}",
        f"持仓市值 {_format_money(market_value)}",
        f"最大购买力 {_format_money(buying_power)}",
        f"维持保证金 {_format_money(maintenance_margin)}",
        f"现金 {_format_money(cash)}",
        f"剩余流动性 {_format_money(remaining_liquidity)}",
    )
    return "账户快照：" + " | ".join(items)


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


def _open_trade_context(cfg: StrategyConfig) -> ft.OpenSecTradeContext:
    """创建绑定美股证券账户的交易上下文，避免 NONE 选中港股模拟账户。"""
    return ft.OpenSecTradeContext(
        filter_trdmarket=ft.TrdMarket.US,
        host=cfg.host,
        port=cfg.port,
    )


def _first_positive_value(row: object, fields: tuple[str, ...]) -> float:
    """按字段顺序读取首个正数值，缺失或非数字按 0 处理。"""
    getter = getattr(row, "get", None)
    if not callable(getter):
        return 0.0
    for field in fields:
        try:
            value = float(getter(field, 0) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value
    return 0.0


def _infer_tranches_from_qty(qty: float, cfg: StrategyConfig) -> int:
    """按当前固定手数设置估算已买入批次，至少 1 批。"""
    if cfg.order_lots_per_trade <= 0:
        return 1
    tranches = int(round(qty / max(1, cfg.order_lots_per_trade)))
    return min(max(1, tranches), max(1, cfg.entry_tranches))


def _sync_broker_positions(
    data: DataAccess,
    strategy: IPOStrategy,
    store: PositionStore,
    cfg: StrategyConfig,
    saved: dict[str, PositionRecord],
) -> None:
    """用券商当前持仓补齐本地状态，避免超时后成交导致持仓漂移。"""
    ret, df = data.position_list_query()
    if ret != ft.RET_OK:
        logger.warning("同步券商持仓失败: %s", df)
        return
    if df.empty:
        return

    today = market_date(cfg.market_timezone)
    synced = 0
    for _, row in df.iterrows():
        getter = getattr(row, "get", None)
        if not callable(getter):
            continue
        code = str(getter("code", "")).strip()
        qty = _first_positive_value(row, ("qty",))
        if not code.startswith("US.") or qty <= 0:
            continue
        cost = _first_positive_value(
            row,
            ("cost_price", "costPrice", "cost_price_valid", "average_cost_price"),
        )
        if cost <= 0:
            continue
        price = _first_positive_value(row, ("price", "last_price"))
        existing = saved.get(code)
        buy_date = existing.buy_date if existing else today
        inferred_tranches = _infer_tranches_from_qty(qty, cfg)
        tranches = max(existing.tranches_bought if existing else 1, inferred_tranches)
        tranches = min(tranches, max(1, cfg.entry_tranches))
        peak_price = max(existing.peak_price if existing else 0.0, cost, price)
        record = PositionRecord(
            code=code,
            cost_price=cost,
            buy_date=buy_date,
            tranches_bought=tranches,
            peak_price=peak_price,
            qty=qty,
        )
        strategy.restore_position(
            code=record.code,
            avg_cost=record.cost_price,
            qty=record.qty,
            buy_date=record.buy_date,
            tranches_bought=record.tranches_bought,
            peak_price=record.peak_price,
        )
        store.save(record)
        saved[code] = record
        synced += 1
    if synced:
        logger.info("已同步券商持仓 %d 只到本地状态", synced)


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
    trade_ctx = _open_trade_context(cfg)

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

    _sync_broker_positions(data, strategy, store, cfg, saved)

    events: queue.Queue = queue.Queue()
    baseline_set_date: dict[str, date] = {}
    failure_alert_gate = _FailureAlertGate(cfg.trade_failure_alert_cooldown_s)

    def send_failure_alert(event: str, code: str, message: str) -> None:
        """发送失败提醒；同一事件/标的在冷却期内只写日志。"""
        if failure_alert_gate.should_send(event, code):
            alerts.send(event, message)
            return
        logger.info("失败提醒冷却中，跳过 %s %s: %s", event, code, message)

    def process_quote(code: str, price: float) -> None:
        """单线程消费：评估并执行。仅此函数会改写策略状态与下单。"""
        if not _is_market_open(cfg):
            return

        # 每个交易日开盘后首次：注入熔断基准净值
        today = market_date(cfg.market_timezone)
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
            display = _get_symbol_display(data, code)
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
                account_snapshot = _account_snapshot_text(data)
                alerts.send(
                    "买入成功",
                    _buy_alert_message(
                        display=display,
                        signal_price=price,
                        lot_size=lot_size,
                        cfg=cfg,
                        decision_reason=decision.reason,
                        result="已成交",
                        detail=(
                            f"成交数量：qty={filled}\n"
                            f"成交均价：{fill_price:.3f}\n"
                            f"批次：第{strategy.get_tranches_bought(code)}/"
                            f"{cfg.entry_tranches}批"
                        ),
                        account_snapshot=account_snapshot,
                    ),
                )
            else:
                reason = trader.last_failure_reason or "买入未执行或未成交"
                account_snapshot = _account_snapshot_text(data)
                message = _buy_alert_message(
                    display=display,
                    signal_price=price,
                    lot_size=lot_size,
                    cfg=cfg,
                    decision_reason=decision.reason,
                    result="未执行",
                    detail=f"原因：{reason}",
                    account_snapshot=account_snapshot,
                )
                if _should_suppress_buy_failure_alert(reason):
                    logger.info("资金不足类买入未执行，仅记录日志 %s: %s", code, message)
                    return
                send_failure_alert(
                    "买入未执行",
                    code,
                    message,
                )

        elif decision.signal == Signal.SELL:
            display = _get_symbol_display(data, code)
            broker_qty = trader.get_position_qty(code)
            if _should_ignore_unheld_sell(strategy.has_position(code), broker_qty):
                logger.info("无持仓，忽略卖出信号 %s: %s", code, decision.reason)
                return
            if trader.sell(code, price):
                strategy.clear_position(code)
                store.delete(code)
                account_snapshot = _account_snapshot_text(data)
                alerts.send(
                    "卖出",
                    _sell_alert_message(
                        display=display,
                        signal_price=price,
                        decision_reason=decision.reason,
                        result="已提交并确认成交",
                        detail="原因：卖出信号触发，执行清仓",
                        account_snapshot=account_snapshot,
                    ),
                )
            else:
                reason = trader.last_failure_reason or "卖出未执行或未成交"
                account_snapshot = _account_snapshot_text(data)
                send_failure_alert(
                    "卖出失败",
                    code,
                    _sell_alert_message(
                        display=display,
                        signal_price=price,
                        decision_reason=decision.reason,
                        result="未完成",
                        detail=f"原因：{reason}",
                        account_snapshot=account_snapshot,
                    ),
                )

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
    # 自选 universe（非 IPO 的通用美股）：启动即订阅，全程纳入分析
    if cfg.watchlist:
        monitor.subscribe(list(cfg.watchlist))
        logger.info("自选 universe %d 只: %s", len(cfg.watchlist), list(cfg.watchlist))

    try:
        while True:
            pv = trader.get_portfolio_value()
            if pv > 0:
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
                set(ipo_map.keys()) | set(cfg.watchlist) | strategy.get_active_codes()
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
