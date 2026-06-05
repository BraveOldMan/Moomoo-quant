# -*- coding: utf-8 -*-
"""交易日时区与行情订阅管理回归测试。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import moomoo as ft

from hk_strategy.clock import market_date
from hk_strategy.monitor import RealtimeMonitor


def test_market_date_uses_new_york_not_local_calendar() -> None:
    shanghai_midnight = datetime(
        2026, 6, 4, 2, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )

    assert market_date("America/New_York", now=shanghai_midnight) == date(
        2026, 6, 3
    )


def test_monitor_unsubscribe_uses_all_sub_types() -> None:
    class _Quote:
        def __init__(self) -> None:
            self.unsubscribed: list[tuple[list[str], list]] = []

        def subscribe(self, codes: list[str], sub_types: list) -> tuple[int, str]:
            return ft.RET_OK, "ok"

        def unsubscribe(self, codes: list[str], sub_types: list) -> tuple[int, str]:
            self.unsubscribed.append((codes, sub_types))
            return ft.RET_OK, "ok"

    quote = _Quote()
    monitor = RealtimeMonitor(
        quote, lambda _code, _price: None, extra_sub_types=[ft.SubType.TICKER]
    )

    monitor.subscribe(["US.AAPL"])
    monitor.unsubscribe(["US.AAPL"])

    assert quote.unsubscribed == [
        (["US.AAPL"], [ft.SubType.QUOTE, ft.SubType.TICKER])
    ]
