# -*- coding: utf-8 -*-
"""交易执行确认回归测试。"""

from hk_strategy.config import StrategyConfig
from hk_strategy.trader import Trader


def test_missing_order_id_is_not_treated_as_full_fill() -> None:
    trader = Trader(
        trade_ctx=object(),  # type: ignore[arg-type]
        data=object(),  # type: ignore[arg-type]
        config=StrategyConfig(order_fill_timeout_s=0.01, order_poll_interval_s=0.01),
    )

    assert trader._poll_fill("", fallback_price=12.34, want_qty=100) == (0.0, 0)
