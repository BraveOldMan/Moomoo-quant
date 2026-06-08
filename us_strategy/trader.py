# -*- coding: utf-8 -*-
import logging
import math
import time
from dataclasses import dataclass

import moomoo as ft

from . import features
from .config import StrategyConfig
from .data_access import DataAccess

logger = logging.getLogger(__name__)

_CANCEL_FAILURE_FILLED_MARKERS = ("已成交", "FILLED_ALL", "FILLED")


@dataclass(frozen=True)
class ExecutionQualityRecord:
    """One confirmed order attempt for execution-quality diagnostics."""

    code: str
    side: str
    order_type: str
    requested_qty: int
    filled_qty: int
    reference_price: float
    limit_price: float
    fill_price: float
    order_id: str
    status: str
    slippage_bps: float | None


class Trader:
    """下单执行：marketable-limit 限价保护 + 成交轮询确认。

    查询类调用走 DataAccess 缓存，下单后主动失效持仓/账户缓存。
    """

    def __init__(
        self,
        trade_ctx: ft.OpenSecTradeContext,
        data: DataAccess,
        config: StrategyConfig,
    ):
        self._ctx = trade_ctx
        self._data = data
        self._cfg = config
        self._trd_env = (
            ft.TrdEnv.REAL if config.trd_env == "REAL" else ft.TrdEnv.SIMULATE
        )
        self._execution_quality: list[ExecutionQualityRecord] = []
        self._last_failure_reason = ""

    @property
    def execution_quality_records(self) -> tuple[ExecutionQualityRecord, ...]:
        """Return execution-quality records collected since process start."""

        return tuple(self._execution_quality)

    @property
    def last_failure_reason(self) -> str:
        """返回最近一次执行失败原因，用于告警展示。"""
        return self._last_failure_reason

    # ── 查询 ────────────────────────────────────────────────────────────
    def _positions(self):
        ret, df = self._data.position_list_query()
        if ret != ft.RET_OK or df.empty:
            return None
        return df

    def get_position_qty(self, code: str) -> int:
        df = self._positions()
        if df is None:
            return 0
        pos = df.set_index("code")
        if code not in pos.index:
            return 0
        return int(pos.loc[code, "qty"])

    def count_open_positions(self) -> int:
        df = self._positions()
        if df is None:
            return 0
        return int((df["qty"] > 0).sum())

    def get_portfolio_value(self) -> float:
        ret, df = self._data.accinfo_query()
        if ret != ft.RET_OK or df.empty:
            return 0.0
        row = df.iloc[0]
        for field in ("net_assets", "total_assets", "net_cash_value"):
            try:
                v = float(row.get(field) or 0)
                if v > 0:
                    return v
            except (TypeError, ValueError):
                continue
        return 0.0

    # ── 下单 ────────────────────────────────────────────────────────────
    def buy(
        self,
        code: str,
        current_price: float,
        lot_size: int,
        atr: float | None = None,
        is_new_position: bool = True,
        position_ratio: float | None = None,
        entry_tranches: int | None = None,
    ) -> tuple[bool, float, int]:
        """买入一批。返回 (是否成功, 成交均价, 成交数量)。

        - 新开仓受 max_positions 限制；max_positions <= 0 表示不限制。
        - 加仓不受 max_positions 限制。
        - use_atr_sizing 时按 ATR 风险预算定量，否则按净值比例/批数定量。
        - position_ratio/entry_tranches 仅用于 IPO 等独立仓位 profile。
        """
        self._last_failure_reason = ""
        if (
            is_new_position
            and self._cfg.max_positions > 0
            and self.count_open_positions() >= self._cfg.max_positions
        ):
            self._last_failure_reason = f"已达最大持仓数 {self._cfg.max_positions}"
            logger.warning(
                "已达最大持仓数 %d，跳过新开仓 %s", self._cfg.max_positions, code
            )
            return False, 0.0, 0

        ret, acc_df = self._data.accinfo_query()
        if ret != ft.RET_OK or acc_df.empty:
            self._last_failure_reason = f"账户信息查询失败: {acc_df}"
            logger.error("accinfo_query 失败: %s", acc_df)
            return False, 0.0, 0

        row = acc_df.iloc[0]
        power = _buying_power_from_account(row, self._cfg.trd_env)
        net_value = self.get_portfolio_value()

        qty = self._size_position(
            current_price,
            lot_size,
            atr,
            power,
            net_value,
            position_ratio=position_ratio,
            entry_tranches=entry_tranches,
        )
        if qty <= 0:
            self._last_failure_reason = _zero_quantity_reason(
                current_price,
                lot_size,
                atr,
                power,
                net_value,
                self._cfg,
                position_ratio=position_ratio,
                entry_tranches=entry_tranches,
            )
            logger.warning(
                "%s，跳过买入 %s (price=%.3f)",
                self._last_failure_reason,
                code,
                current_price,
            )
            return False, 0.0, 0

        # marketable-limit：愿意以略高于现价成交，兼顾成交率与滑点控制
        limit_price = self._limit_price(current_price, is_buy=True)
        ok, fill_price, filled = self._place_and_confirm(
            code,
            qty,
            ft.TrdSide.BUY,
            limit_price,
            reference_price=current_price,
        )
        if ok:
            logger.info("买入成功: %s qty=%d 成交均价=%.3f", code, filled, fill_price)
        return ok, fill_price, filled

    def sell(self, code: str, current_price: float) -> bool:
        """清仓指定股票（marketable-limit）。"""
        self._last_failure_reason = ""
        qty = self.get_position_qty(code)
        if qty <= 0:
            self._last_failure_reason = "无持仓可卖"
            logger.info("无持仓可卖: %s", code)
            return False

        limit_price = self._limit_price(current_price, is_buy=False)
        ok, fill_price, filled = self._place_and_confirm(
            code,
            qty,
            ft.TrdSide.SELL,
            limit_price,
            reference_price=current_price,
        )
        if ok:
            logger.info("卖出成功: %s qty=%d 成交均价=%.3f", code, filled, fill_price)
        return ok

    # ── 内部 ────────────────────────────────────────────────────────────
    def _size_position(
        self,
        price: float,
        lot_size: int,
        atr: float | None,
        power: float,
        net_value: float,
        position_ratio: float | None = None,
        entry_tranches: int | None = None,
    ) -> int:
        cfg = self._cfg
        lot = max(1, int(lot_size or 1))
        if cfg.order_lots_per_trade > 0:
            qty = lot * cfg.order_lots_per_trade
            if price <= 0 or qty * price > power:
                return 0
            return qty
        if cfg.use_atr_sizing and atr and atr > 0 and net_value > 0:
            sized = features.atr_position_size(
                net_value,
                price,
                atr,
                cfg.atr_risk_per_trade_pct,
                cfg.atr_stop_multiple,
                lot_size,
            )
            qty = sized.qty
            # 不得超买（受购买力限制）
            if price > 0:
                qty = min(qty, int(power / price))
        else:
            sizing_base = net_value if net_value > 0 else power
            ratio = cfg.position_ratio if position_ratio is None else position_ratio
            tranches = cfg.entry_tranches if entry_tranches is None else entry_tranches
            tranche_budget = sizing_base * ratio / max(1, tranches)
            qty = int(math.floor(tranche_budget / price)) if price > 0 else 0
            if price > 0:
                qty = min(qty, int(power / price))
        return (qty // lot) * lot

    def _limit_price(self, current_price: float, is_buy: bool) -> float:
        tol = self._cfg.limit_price_tolerance_pct
        if not self._cfg.use_limit_orders:
            return 0.0  # 0 → 市价单
        price = current_price * (1 + tol) if is_buy else current_price * (1 - tol)
        return round(price, 2)

    def _place_and_confirm(
        self,
        code: str,
        qty: int,
        side,
        limit_price: float,
        reference_price: float,
    ) -> tuple[bool, float, int]:
        use_limit = self._cfg.use_limit_orders and limit_price > 0
        order_type = ft.OrderType.NORMAL if use_limit else ft.OrderType.MARKET
        price = limit_price if use_limit else 0

        ret, data = self._ctx.place_order(
            price=price,
            qty=qty,
            code=code,
            trd_side=side,
            order_type=order_type,
            trd_env=self._trd_env,
        )
        self._data.on_order_changed()
        if ret != ft.RET_OK:
            self._last_failure_reason = f"下单接口失败: {data}"
            logger.error("下单失败 %s %s: %s", side, code, data)
            self._record_execution_quality(
                code=code,
                side=str(side),
                order_type=str(order_type),
                requested_qty=qty,
                filled_qty=0,
                reference_price=reference_price,
                limit_price=price,
                fill_price=0.0,
                order_id="",
                status="SUBMIT_FAILED",
            )
            return False, 0.0, 0

        order_id = _extract(data, "order_id", "")
        fill_price, filled, status = self._poll_fill_detail(
            order_id,
            reference_price,
            qty,
        )
        if status.startswith("TIMEOUT:") and filled < qty:
            cancel_status = self._cancel_order(order_id)
            status = f"{status};{cancel_status}"
            latest_price, latest_filled, latest_status = self._query_order_once(
                order_id,
                fallback_price=fill_price,
                fallback_filled=filled,
            )
            if latest_filled > filled or _is_filled_status(latest_status):
                fill_price = latest_price
                # 优先采用重查到的真实成交量；查不到（为 0）才回退请求量。
                filled = latest_filled if latest_filled > 0 else qty
                status = f"{latest_status};{cancel_status}"
            elif _cancel_failure_implies_fill(cancel_status):
                fill_price = latest_price
                # 撤单失败暗示已成交：若重查到真实部分成交量则采用之，
                # 避免无条件按 qty 全额计账造成超量持仓。
                filled = latest_filled if latest_filled > 0 else qty
                status = f"FILLED_ASSUMED_AFTER_CANCEL_FAILED;{cancel_status}"
        self._record_execution_quality(
            code=code,
            side=str(side),
            order_type=str(order_type),
            requested_qty=qty,
            filled_qty=filled,
            reference_price=reference_price,
            limit_price=price,
            fill_price=fill_price,
            order_id=str(order_id or ""),
            status=status,
        )
        if filled <= 0:
            self._last_failure_reason = (
                f"订单未成交或超时: status={status}, order_id={order_id}"
            )
        return (filled > 0), fill_price, filled

    def _cancel_order(self, order_id: object) -> str:
        """撤销超时未全成订单，返回用于执行质量记录的状态片段。"""
        if not order_id:
            return "CANCEL_SKIPPED:MISSING_ORDER_ID"
        ret, data = self._ctx.modify_order(
            ft.ModifyOrderOp.CANCEL,
            order_id=order_id,
            qty=0,
            price=0,
            trd_env=self._trd_env,
        )
        self._data.on_order_changed()
        if ret == ft.RET_OK:
            logger.info("订单 %s 超时后已发送撤单", order_id)
            return "CANCEL_SENT"
        logger.error("订单 %s 超时撤单失败: %s", order_id, data)
        return f"CANCEL_FAILED:{data}"

    def _query_order_once(
        self,
        order_id: object,
        fallback_price: float,
        fallback_filled: int,
    ) -> tuple[float, int, str]:
        """读取一次订单状态，用于撤单失败后的成交竞态复核。"""
        ret, df = self._ctx.order_list_query(
            order_id=order_id,
            trd_env=self._trd_env,
            refresh_cache=True,
        )
        if ret != ft.RET_OK or df.empty:
            return fallback_price, fallback_filled, "ORDER_QUERY_FAILED"
        row = df.iloc[0]
        status = str(row.get("order_status", "")) or "UNKNOWN"
        filled = int(_extract(df, "dealt_qty", fallback_filled) or fallback_filled)
        avg = float(_extract(df, "dealt_avg_price", 0) or 0)
        price = avg if avg > 0 else fallback_price
        return price, filled, status

    def _poll_fill(
        self, order_id, fallback_price: float, want_qty: int
    ) -> tuple[float, int]:
        """轮询订单直至成交/超时。返回 (成交均价, 已成交数量)。"""
        fill_price, filled, _ = self._poll_fill_detail(
            order_id,
            fallback_price,
            want_qty,
        )
        return fill_price, filled

    def _poll_fill_detail(
        self, order_id, fallback_price: float, want_qty: int
    ) -> tuple[float, int, str]:
        """轮询订单直至成交/超时，返回成交价、数量和最终状态。"""
        if not order_id:
            logger.error("下单返回缺少 order_id，无法确认成交，按未成交处理")
            return 0.0, 0, "MISSING_ORDER_ID"
        deadline = time.monotonic() + self._cfg.order_fill_timeout_s
        last_price, last_filled = fallback_price, 0
        last_status = "SUBMITTED"
        while time.monotonic() < deadline:
            ret, df = self._ctx.order_list_query(
                order_id=order_id, trd_env=self._trd_env
            )
            if ret == ft.RET_OK and not df.empty:
                row = df.iloc[0]
                status = str(row.get("order_status", ""))
                last_status = status or last_status
                last_filled = int(_extract(df, "dealt_qty", last_filled) or last_filled)
                avg = float(_extract(df, "dealt_avg_price", 0) or 0)
                if avg > 0:
                    last_price = avg
                if "FILLED_ALL" in status:
                    return last_price, last_filled or want_qty, status
                if "CANCELLED" in status or "FAILED" in status or "DELETED" in status:
                    logger.warning(
                        "订单 %s 状态 %s，已成交 %d", order_id, status, last_filled
                    )
                    return last_price, last_filled, status
            time.sleep(self._cfg.order_poll_interval_s)
        logger.warning(
            "订单 %s 等待成交超时，已成交 %d/%d", order_id, last_filled, want_qty
        )
        return last_price, last_filled, f"TIMEOUT:{last_status}"

    def _record_execution_quality(
        self,
        code: str,
        side: str,
        order_type: str,
        requested_qty: int,
        filled_qty: int,
        reference_price: float,
        limit_price: float,
        fill_price: float,
        order_id: str,
        status: str,
    ) -> None:
        slippage = _slippage_bps(side, reference_price, fill_price, filled_qty)
        record = ExecutionQualityRecord(
            code=code,
            side=side,
            order_type=order_type,
            requested_qty=requested_qty,
            filled_qty=filled_qty,
            reference_price=reference_price,
            limit_price=limit_price,
            fill_price=fill_price,
            order_id=order_id,
            status=status,
            slippage_bps=slippage,
        )
        self._execution_quality.append(record)
        logger.info("执行质量: %s", record)


def _extract(df, column: str, default):
    try:
        return df[column].iloc[0]
    except (KeyError, TypeError, ValueError, IndexError):
        return default


def _slippage_bps(
    side: str,
    reference_price: float,
    fill_price: float,
    filled_qty: int,
) -> float | None:
    if filled_qty <= 0 or reference_price <= 0 or fill_price <= 0:
        return None
    side_upper = side.upper()
    if "BUY" in side_upper:
        return (fill_price - reference_price) / reference_price * 10_000.0
    if "SELL" in side_upper:
        return (reference_price - fill_price) / reference_price * 10_000.0
    return None


def _is_filled_status(status: object) -> bool:
    """判断订单状态是否表示全部成交。"""
    return "FILLED_ALL" in str(status).upper()


def _cancel_failure_implies_fill(cancel_status: object) -> bool:
    """moomoo 撤单失败文案明确表示已成交时，按成交竞态处理。"""
    text = str(cancel_status)
    upper = text.upper()
    return any(
        marker in text or marker in upper for marker in _CANCEL_FAILURE_FILLED_MARKERS
    )


def _positive_float(value: object) -> float:
    """返回正数浮点值；空值、N/A、非数值均按 0 处理。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) and result > 0 else 0.0


def _zero_quantity_reason(
    price: float,
    lot_size: int,
    atr: float | None,
    power: float,
    net_value: float,
    cfg: StrategyConfig,
    position_ratio: float | None = None,
    entry_tranches: int | None = None,
) -> str:
    """解释仓位计算为 0 的原因。"""
    if price <= 0:
        return f"价格无效：price={price:.3f}"
    lot = max(1, int(lot_size or 1))
    min_lot_cash = price * lot
    if cfg.order_lots_per_trade > 0:
        target_qty = lot * cfg.order_lots_per_trade
        target_cash = price * target_qty
        return (
            f"固定{cfg.order_lots_per_trade}手下单资金不足："
            f"计划数量={target_qty}，预计金额={target_cash:.2f}，"
            f"可用资金={power:.2f}，lot_size={lot}"
        )
    if power < min_lot_cash:
        return (
            f"可用资金不足：可用资金={power:.2f}，"
            f"最低一手约={min_lot_cash:.2f}，lot_size={lot}"
        )
    if cfg.use_atr_sizing:
        return (
            f"ATR仓位为0：可用资金={power:.2f}，净值={net_value:.2f}，"
            f"ATR={atr or 0:.3f}，最低一手约={min_lot_cash:.2f}，lot_size={lot}"
        )
    sizing_base = net_value if net_value > 0 else power
    ratio = cfg.position_ratio if position_ratio is None else position_ratio
    tranches = cfg.entry_tranches if entry_tranches is None else entry_tranches
    tranche_budget = sizing_base * ratio / max(1, tranches)
    if tranche_budget < min_lot_cash:
        return (
            f"单批预算不足：单批预算={tranche_budget:.2f}，"
            f"最低一手约={min_lot_cash:.2f}，"
            f"净值={net_value:.2f}，可用资金={power:.2f}，lot_size={lot}"
        )
    return (
        f"整手约束导致数量为0：可用资金={power:.2f}，"
        f"净值={net_value:.2f}，单批预算={tranche_budget:.2f}，"
        f"最低一手约={min_lot_cash:.2f}，lot_size={lot}"
    )


def _account_field(row: object, field: str) -> object:
    """兼容 pandas Series 的字段读取。"""
    getter = getattr(row, "get", None)
    if not callable(getter):
        return None
    return getter(field, None)


def _buying_power_from_account(row: object, trd_env: str) -> float:
    """读取购买力；模拟盘 power 为 0 时回退现金字段，实盘不回退。"""
    power = _positive_float(_account_field(row, "power"))
    if power > 0 or trd_env == "REAL":
        return power

    for field in (
        "available_funds",
        "cash",
        "net_cash_power",
        "net_cash_value",
        "total_assets",
        "net_assets",
    ):
        fallback = _positive_float(_account_field(row, field))
        if fallback > 0:
            logger.warning(
                "账户 power=0，模拟盘使用 %s=%.2f 估算购买力",
                field,
                fallback,
            )
            return fallback
    return 0.0
