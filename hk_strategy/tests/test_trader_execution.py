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
                    "dealt_qty": 100,
                    "dealt_avg_price": 10.05,
                }
            ]
        )
