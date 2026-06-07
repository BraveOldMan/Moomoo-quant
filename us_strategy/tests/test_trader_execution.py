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


class _Data:
    def on_order_changed(self) -> None:
        pass


class _BuyingData:
    def accinfo_query(self):
        return ft.RET_OK, pd.DataFrame([{"power": 5_000.0, "net_assets": 5_000.0}])

    def on_order_changed(self) -> None:
        pass


class _FilledTradeContext:
    def __init__(self) -> None:
        self.place_order_calls = 0

    def place_order(self, **_kwargs):
        self.place_order_calls += 1
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
