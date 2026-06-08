# -*- coding: utf-8 -*-
"""交易执行确认回归测试。"""

import pandas as pd

import moomoo as ft

from hk_strategy.config import StrategyConfig
from hk_strategy.trader import Trader


class _Data:
    def __init__(self, book):
        self._book = book

    def get_order_book(self, *_args, **_kwargs):
        return 0, self._book


def test_missing_order_id_is_not_treated_as_full_fill() -> None:
    trader = Trader(
        trade_ctx=object(),  # type: ignore[arg-type]
        data=object(),  # type: ignore[arg-type]
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    assert trader._poll_fill("", fallback_price=12.34, want_qty=100) == (0.0, 0)


def test_execution_liquidity_gate_blocks_wide_spread() -> None:
    trader = Trader(
        trade_ctx=object(),  # type: ignore[arg-type]
        data=_Data(
            {
                "Bid": [(9.0, 10_000, 1, {})],
                "Ask": [(10.0, 10_000, 1, {})],
            }
        ),  # type: ignore[arg-type]
        config=StrategyConfig(
            use_order_book_metrics=True,
            order_book_spread_danger_bps=30.0,
            order_book_slippage_danger_bps=50.0,
        ),
    )

    assert trader._execution_liquidity_blocked("HK.TEST", 100) is True


def test_execution_liquidity_gate_is_disabled_by_default() -> None:
    trader = Trader(
        trade_ctx=object(),  # type: ignore[arg-type]
        data=_Data({}),  # type: ignore[arg-type]
        config=StrategyConfig(use_order_book_metrics=False),
    )

    assert trader._execution_liquidity_blocked("HK.TEST", 100) is False


def test_max_positions_zero_disables_new_position_limit() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(),  # type: ignore[arg-type]
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=1,
            position_ratio=0.2,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, fill_price, filled = trader.buy(
        "HK.TEST",
        current_price=10.0,
        lot_size=100,
        is_new_position=True,
    )

    assert ok
    assert fill_price == 10.05
    assert filled == 100
    assert trade_ctx.place_order_calls == 1


def test_simulate_buy_uses_cash_when_power_is_zero() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=0.0, cash=5_000.0),  # type: ignore[arg-type]
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
        "HK.TEST",
        current_price=10.0,
        lot_size=100,
        is_new_position=True,
    )

    assert ok
    assert fill_price == 10.05
    assert filled == 100
    assert trade_ctx.place_order_calls == 1


def test_position_ratio_sizes_from_net_assets_not_margin_power() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=200_000.0, net_assets=100_000.0),  # type: ignore[arg-type]
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=2,
            position_ratio=0.06,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, _fill_price, filled = trader.buy(
        "HK.TEST",
        current_price=10.0,
        lot_size=100,
        is_new_position=True,
    )

    assert ok
    assert filled == 100
    assert trade_ctx.last_order["qty"] == 300


def test_position_ratio_zero_disables_new_buy() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=200_000.0, net_assets=100_000.0),  # type: ignore[arg-type]
        config=StrategyConfig(max_positions=0, position_ratio=0.0),
    )

    ok, fill_price, filled = trader.buy(
        "HK.TEST",
        current_price=10.0,
        lot_size=100,
        is_new_position=True,
    )

    assert ok is False
    assert fill_price == 0.0
    assert filled == 0
    assert trade_ctx.place_order_calls == 0
    assert "单批预算不足" in trader.last_failure_reason


def test_ipo_sizing_profile_can_buy_when_regular_ratio_is_zero() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=200_000.0, net_assets=100_000.0),  # type: ignore[arg-type]
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=2,
            position_ratio=0.0,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, _fill_price, _filled = trader.buy(
        "HK.06680",
        current_price=10.0,
        lot_size=100,
        is_new_position=True,
        position_ratio=0.05,
        entry_tranches=2,
    )

    assert ok
    assert trade_ctx.last_order["qty"] == 200


def test_real_buy_does_not_fallback_to_cash_when_power_is_zero() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=0.0, cash=5_000.0),  # type: ignore[arg-type]
        config=StrategyConfig(
            trd_env="REAL",
            max_positions=0,
            entry_tranches=1,
            position_ratio=0.2,
        ),
    )

    ok, fill_price, filled = trader.buy(
        "HK.TEST",
        current_price=10.0,
        lot_size=100,
        is_new_position=True,
    )

    assert ok is False
    assert fill_price == 0.0
    assert filled == 0
    assert trade_ctx.place_order_calls == 0


def test_order_lots_per_trade_forces_one_board_lot() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=169_360.26),  # type: ignore[arg-type]
        config=StrategyConfig(
            max_positions=0,
            entry_tranches=2,
            position_ratio=0.2,
            order_lots_per_trade=1,
            order_fill_timeout_s=0.01,
            order_poll_interval_s=0.01,
        ),
    )

    ok, _fill_price, filled = trader.buy(
        "HK.03750",
        current_price=688.5,
        lot_size=100,
        is_new_position=True,
    )

    assert ok
    assert filled == 100
    assert trade_ctx.last_order["qty"] == 100


def test_order_lots_per_trade_reports_cash_shortfall() -> None:
    trade_ctx = _FilledTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_BuyingData(power=50_000.0),  # type: ignore[arg-type]
        config=StrategyConfig(max_positions=0, order_lots_per_trade=1),
    )

    ok, fill_price, filled = trader.buy(
        "HK.03750",
        current_price=688.5,
        lot_size=100,
        is_new_position=True,
    )

    assert ok is False
    assert fill_price == 0.0
    assert filled == 0
    assert trade_ctx.place_order_calls == 0
    assert "固定1手下单资金不足" in trader.last_failure_reason


def test_timeout_order_is_cancelled_and_reports_status() -> None:
    trade_ctx = _TimeoutTradeContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_OrderData(),  # type: ignore[arg-type]
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "HK.TEST",
        100,
        ft.TrdSide.BUY,
        10.1,
        fallback=10.0,
    )

    assert ok is False
    assert fill_price == 10.0
    assert filled == 0
    assert trade_ctx.cancel_calls == 1
    assert trade_ctx.last_cancel["modify_order_op"] == ft.ModifyOrderOp.CANCEL
    assert trade_ctx.last_cancel["qty"] == 0
    assert trade_ctx.last_cancel["price"] == 0
    assert "TIMEOUT:SUBMITTED;CANCEL_SENT" in trader.last_failure_reason


def test_late_fill_after_cancel_failure_is_treated_as_filled() -> None:
    trade_ctx = _LateFillCancelFailureContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_OrderData(),  # type: ignore[arg-type]
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "HK.TEST",
        100,
        ft.TrdSide.BUY,
        10.1,
        fallback=10.0,
    )

    assert ok
    assert fill_price == 10.08
    assert filled == 100
    assert trade_ctx.cancel_calls == 1
    assert trader.last_failure_reason == ""


def test_cancel_failure_filled_message_is_treated_as_filled_when_query_stale() -> None:
    trade_ctx = _CancelFailedStaleFilledContext()
    trader = Trader(
        trade_ctx=trade_ctx,  # type: ignore[arg-type]
        data=_OrderData(),  # type: ignore[arg-type]
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    ok, fill_price, filled = trader._place_and_confirm(
        "HK.TEST",
        100,
        ft.TrdSide.BUY,
        10.1,
        fallback=10.0,
    )

    assert ok
    assert fill_price == 10.0
    assert filled == 100
    assert trade_ctx.cancel_calls == 1
    assert trader.last_failure_reason == ""


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


class _OrderData:
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
                    "dealt_qty": 100,
                    "dealt_avg_price": 10.05,
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
                        "dealt_qty": 100,
                        "dealt_avg_price": 10.08,
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
