# -*- coding: utf-8 -*-
"""交易执行确认回归测试。"""

import pandas as pd

import moomoo as ft

from us_strategy.config import StrategyConfig
from us_strategy.trader import Trader


def test_missing_order_id_is_not_treated_as_full_fill() -> None:
    trader = Trader(
        trade_ctx=object(),  # type: ignore[arg-type]
        data=object(),  # type: ignore[arg-type]
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    assert trader._poll_fill("", fallback_price=12.34, want_qty=100) == (0.0, 0)


def test_execution_quality_record_captures_fill_slippage() -> None:
    trader = Trader(
        trade_ctx=_FilledTradeContext(),
        data=_Data(),
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "US.X",
        10,
        ft.TrdSide.BUY,
        100.7,
        reference_price=100.0,
    )

    record = trader.execution_quality_records[-1]
    assert ok
    assert fill_price == 100.5
    assert filled == 10
    assert record.code == "US.X"
    assert record.filled_qty == 10
    assert record.limit_price == 100.7
    assert record.slippage_bps == 50.0


def test_max_positions_zero_disables_new_position_limit() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(),
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=1,
            position_ratio=0.2,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, fill_price, filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok
    assert fill_price == 100.5
    assert filled == 10
    assert trade_ctx.place_order_calls == 1


def test_simulate_buy_uses_cash_when_power_is_zero() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(power=0.0, cash=5_000.0),
        config=StrategyConfig(
            trd_env="SIMULATE",
            max_positions=0,
            entry_tranches=1,
            position_ratio=0.2,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, fill_price, filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok
    assert fill_price == 100.5
    assert filled == 10
    assert trade_ctx.place_order_calls == 1


def test_position_ratio_sizes_from_net_assets_not_margin_power() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(power=200_000.0, net_assets=100_000.0),
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=2,
            position_ratio=0.06,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, _fill_price, _filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok
    assert trade_ctx.last_order["qty"] == 30


def test_position_ratio_zero_disables_new_buy() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(power=200_000.0, net_assets=100_000.0),
        config=StrategyConfig(max_positions=0, position_ratio=0.0),
    )

    ok, fill_price, filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok is False
    assert fill_price == 0.0
    assert filled == 0
    assert trade_ctx.place_order_calls == 0
    assert "单批预算不足" in trader.last_failure_reason


def test_real_buy_does_not_fallback_to_cash_when_power_is_zero() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(power=0.0, cash=5_000.0),
        config=StrategyConfig(
            trd_env="REAL",
            max_positions=0,
            entry_tranches=1,
            position_ratio=0.2,
        ),
    )

    ok, fill_price, filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok is False
    assert fill_price == 0.0
    assert filled == 0
    assert trade_ctx.place_order_calls == 0


def test_order_lots_per_trade_forces_one_share_lot() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(power=5_000.0),
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=2,
            position_ratio=0.001,
            order_lots_per_trade=1,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, _fill_price, _filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok
    assert trade_ctx.last_order["qty"] == 1


def test_order_lots_per_trade_reports_cash_shortfall() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_BuyingData(power=50.0),
        config=StrategyConfig(max_positions=0, order_lots_per_trade=1),
    )

    ok, fill_price, filled = trader.buy(
        "US.X",
        current_price=100.0,
        lot_size=1,
        is_new_position=True,
    )

    assert ok is False
    assert fill_price == 0.0
    assert filled == 0
    assert trade_ctx.place_order_calls == 0
    assert "固定1手下单资金不足" in trader.last_failure_reason


def test_us_limit_price_uses_cent_precision() -> None:
    trader = Trader(
        trade_ctx=_FilledTradeContext(),
        data=_BuyingData(),
        config=StrategyConfig(limit_price_tolerance_pct=0.005),
    )

    assert trader._limit_price(742.5891, is_buy=True) == 746.3
    assert trader._limit_price(742.5891, is_buy=False) == 738.88


def test_timeout_order_is_cancelled_and_recorded() -> None:
    trade_ctx = _TimeoutTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_Data(),
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "US.X",
        1,
        ft.TrdSide.BUY,
        101.0,
        reference_price=100.0,
    )

    record = trader.execution_quality_records[-1]
    assert ok is False
    assert fill_price == 100.0
    assert filled == 0
    assert trade_ctx.cancel_calls == 1
    assert trade_ctx.last_cancel["modify_order_op"] == ft.ModifyOrderOp.CANCEL
    assert trade_ctx.last_cancel["qty"] == 0
    assert trade_ctx.last_cancel["price"] == 0
    assert "TIMEOUT:SUBMITTED;CANCEL_SENT" == record.status
    assert "CANCEL_SENT" in trader.last_failure_reason


def test_late_fill_after_cancel_failure_is_treated_as_filled() -> None:
    trade_ctx = _LateFillCancelFailureContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_Data(),
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "US.X",
        1,
        ft.TrdSide.BUY,
        101.0,
        reference_price=100.0,
    )

    record = trader.execution_quality_records[-1]
    assert ok
    assert fill_price == 100.8
    assert filled == 1
    assert trade_ctx.cancel_calls == 1
    assert record.status == "FILLED_ALL;CANCEL_FAILED:订单已成交，无法执行操作"
    assert trader.last_failure_reason == ""


def test_cancel_failure_filled_message_is_treated_as_filled_when_query_stale() -> None:
    trade_ctx = _CancelFailedStaleFilledContext()
    trader = Trader(
        trade_ctx=trade_ctx,
        data=_Data(),
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "US.X",
        1,
        ft.TrdSide.BUY,
        101.0,
        reference_price=100.0,
    )

    record = trader.execution_quality_records[-1]
    assert ok
    assert fill_price == 100.0
    assert filled == 1
    assert record.status.startswith("FILLED_ASSUMED_AFTER_CANCEL_FAILED")
    assert trader.last_failure_reason == ""


class _Data:
    def on_order_changed(self) -> None:
        pass


class _BuyingData:
    def __init__(
        self,
        power: float = 5_000.0,
        cash: float = 0.0,
        net_assets: float = 5_000.0,
    ) -> None:
        self._power = power
        self._cash = cash
        self._net_assets = net_assets

    def accinfo_query(self):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "power": self._power,
                    "cash": self._cash,
                    "net_assets": self._net_assets,
                }
            ]
        )

    def on_order_changed(self) -> None:
        pass


class _FilledTradeContext:
    def __init__(self) -> None:
        self.place_order_calls = 0
        self.last_order = {}

    def place_order(self, **kwargs):
        self.place_order_calls += 1
        self.last_order = kwargs
        return ft.RET_OK, pd.DataFrame([{"order_id": "ord-1"}])

    def order_list_query(self, **_kwargs):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "order_status": "FILLED_ALL",
                    "dealt_qty": 10,
                    "dealt_avg_price": 100.5,
                }
            ]
        )


class _TimeoutTradeContext(_FilledTradeContext):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_calls = 0
        self.last_cancel = {}

    def order_list_query(self, **_kwargs):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "order_status": "SUBMITTED",
                    "dealt_qty": 0,
                    "dealt_avg_price": 0,
                }
            ]
        )

    def modify_order(self, modify_order_op, **kwargs):
        self.cancel_calls += 1
        kwargs["modify_order_op"] = modify_order_op
        self.last_cancel = kwargs
        return ft.RET_OK, pd.DataFrame([{"order_id": "ord-1"}])


class _LateFillCancelFailureContext(_TimeoutTradeContext):
    def __init__(self) -> None:
        super().__init__()
        self._after_cancel = False

    def order_list_query(self, **kwargs):
        if self._after_cancel:
            return ft.RET_OK, pd.DataFrame(
                [
                    {
                        "order_status": "FILLED_ALL",
                        "dealt_qty": 1,
                        "dealt_avg_price": 100.8,
                    }
                ]
            )
        return super().order_list_query(**kwargs)

    def modify_order(self, modify_order_op, **kwargs):
        self.cancel_calls += 1
        kwargs["modify_order_op"] = modify_order_op
        self.last_cancel = kwargs
        self._after_cancel = True
        return ft.RET_ERROR, "订单已成交，无法执行操作"


class _CancelFailedStaleFilledContext(_TimeoutTradeContext):
    def modify_order(self, modify_order_op, **kwargs):
        self.cancel_calls += 1
        kwargs["modify_order_op"] = modify_order_op
        self.last_cancel = kwargs
        return ft.RET_ERROR, "订单已成交，无法执行操作"
