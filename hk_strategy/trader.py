# -*- coding: utf-8 -*-
import logging
import math
import time

import moomoo as ft

from . import features
from .config import StrategyConfig
from .data_access import DataAccess

logger = logging.getLogger(__name__)


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
    ) -> tuple[bool, float, int]:
        """买入一批。返回 (是否成功, 成交均价, 成交数量)。

        - 新开仓受 max_positions 限制；加仓不受限。
        - use_atr_sizing 时按 ATR 风险预算定量，否则按 position_ratio/批数。
        """
        if is_new_position and self.count_open_positions() >= self._cfg.max_positions:
            logger.warning(
                "已达最大持仓数 %d，跳过新开仓 %s", self._cfg.max_positions, code
            )
            return False, 0.0, 0

        ret, acc_df = self._data.accinfo_query()
        if ret != ft.RET_OK or acc_df.empty:
            logger.error("accinfo_query 失败: %s", acc_df)
            return False, 0.0, 0

        try:
            power = float(acc_df["power"].iloc[0])
        except (KeyError, TypeError, ValueError, IndexError):
            power = 0.0
        net_value = self.get_portfolio_value()

        qty = self._size_position(current_price, lot_size, atr, power, net_value)
        if qty <= 0:
            logger.warning(
                "资金不足或仓位为零，跳过买入 %s (price=%.3f)", code, current_price
            )
            return False, 0.0, 0

        # marketable-limit：愿意以略高于现价成交，兼顾成交率与滑点控制
        limit_price = self._limit_price(current_price, is_buy=True)
        ok, fill_price, filled = self._place_and_confirm(
            code, qty, ft.TrdSide.BUY, limit_price, fallback=current_price
        )
        if ok:
            logger.info("买入成功: %s qty=%d 成交均价=%.3f", code, filled, fill_price)
        return ok, fill_price, filled

    def sell(self, code: str, current_price: float) -> bool:
        """清仓指定股票（marketable-limit）。"""
        qty = self.get_position_qty(code)
        if qty <= 0:
            logger.info("无持仓可卖: %s", code)
            return False

        limit_price = self._limit_price(current_price, is_buy=False)
        ok, fill_price, filled = self._place_and_confirm(
            code, qty, ft.TrdSide.SELL, limit_price, fallback=current_price
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
    ) -> int:
        cfg = self._cfg
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
            tranche_budget = power * cfg.position_ratio / cfg.entry_tranches
            qty = int(math.floor(tranche_budget / price)) if price > 0 else 0
        return (qty // lot_size) * lot_size

    def _limit_price(self, current_price: float, is_buy: bool) -> float:
        tol = self._cfg.limit_price_tolerance_pct
        if not self._cfg.use_limit_orders:
            return 0.0  # 0 → 市价单
        if is_buy:
            return round(current_price * (1 + tol), 3)
        return round(current_price * (1 - tol), 3)

    def _place_and_confirm(
        self, code: str, qty: int, side, limit_price: float, fallback: float
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
            logger.error("下单失败 %s %s: %s", side, code, data)
            return False, 0.0, 0

        order_id = _extract(data, "order_id", "")
        fill_price, filled = self._poll_fill(order_id, fallback, qty)
        return (filled > 0), fill_price, filled

    def _poll_fill(
        self, order_id, fallback_price: float, want_qty: int
    ) -> tuple[float, int]:
        """轮询订单直至成交/超时。返回 (成交均价, 已成交数量)。"""
        if not order_id:
            logger.error("下单返回缺少 order_id，无法确认成交，按未成交处理")
            return 0.0, 0
        deadline = time.monotonic() + self._cfg.order_fill_timeout_s
        last_price, last_filled = fallback_price, 0
        while time.monotonic() < deadline:
            ret, df = self._ctx.order_list_query(
                order_id=order_id, trd_env=self._trd_env
            )
            if ret == ft.RET_OK and not df.empty:
                row = df.iloc[0]
                status = str(row.get("order_status", ""))
                last_filled = int(_extract(df, "dealt_qty", last_filled) or last_filled)
                avg = float(_extract(df, "dealt_avg_price", 0) or 0)
                if avg > 0:
                    last_price = avg
                if "FILLED_ALL" in status:
                    return last_price, last_filled or want_qty
                if "CANCELLED" in status or "FAILED" in status or "DELETED" in status:
                    logger.warning(
                        "订单 %s 状态 %s，已成交 %d", order_id, status, last_filled
                    )
                    return last_price, last_filled
            time.sleep(self._cfg.order_poll_interval_s)
        logger.warning(
            "订单 %s 等待成交超时，已成交 %d/%d", order_id, last_filled, want_qty
        )
        return last_price, last_filled


def _extract(df, column: str, default):
    try:
        return df[column].iloc[0]
    except (KeyError, TypeError, ValueError, IndexError):
        return default
