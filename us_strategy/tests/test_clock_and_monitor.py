# -*- coding: utf-8 -*-
"""交易日时区与行情订阅管理回归测试。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import moomoo as ft
import pandas as pd

from us_strategy.clock import market_date
from us_strategy.config import StrategyConfig
from us_strategy.forward_monitor import (
    _market_session,
    _parse_market_sessions,
    _session_price,
    _should_log_session,
    _subscribe_forward_quotes,
)
from us_strategy.monitor import RealtimeMonitor


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


def test_forward_monitor_market_session_windows() -> None:
    cfg = StrategyConfig()
    assert _market_session(cfg, datetime(2026, 6, 5, 8, 0, tzinfo=ZoneInfo("UTC"))) == "PRE"
    assert _market_session(cfg, datetime(2026, 6, 5, 14, 0, tzinfo=ZoneInfo("UTC"))) == "RTH"
    assert _market_session(cfg, datetime(2026, 6, 5, 21, 0, tzinfo=ZoneInfo("UTC"))) == "AFTER"
    assert _market_session(cfg, datetime(2026, 6, 6, 14, 0, tzinfo=ZoneInfo("UTC"))) == "CLOSED"


def test_forward_monitor_session_filters() -> None:
    sessions = _parse_market_sessions("pre,after,unknown")

    assert sessions == {"PRE", "AFTER"}
    assert _should_log_session("PRE", sessions, ignore_hours=False)
    assert not _should_log_session("RTH", sessions, ignore_hours=False)
    assert _should_log_session("RTH", set(), ignore_hours=False)
    assert not _should_log_session("PRE", set(), ignore_hours=False)


def test_forward_monitor_extended_subscribe_uses_eth_session() -> None:
    class _Quote:
        def __init__(self) -> None:
            self.kwargs = {}

        def subscribe(self, _codes, _sub_types, **kwargs) -> tuple[int, str]:
            self.kwargs = kwargs
            return ft.RET_OK, "ok"

    quote = _Quote()
    _subscribe_forward_quotes(quote, ["US.AAPL"], {"PRE", "AFTER"})

    assert quote.kwargs["extended_time"] is True
    assert quote.kwargs["session"] == ft.Session.ETH


def test_forward_monitor_session_price_uses_extended_fields() -> None:
    class _Data:
        def __init__(self) -> None:
            self.ticker_calls = 0

        def get_market_snapshot(self, _code: str) -> tuple[int, pd.DataFrame]:
            return ft.RET_OK, pd.DataFrame(
                [
                    {
                        "pre_price": 101.0,
                        "after_price": 202.0,
                        "last_price": 999.0,
                    }
                ]
            )

        def get_rt_ticker(self, _code: str, _num: int) -> tuple[int, pd.DataFrame]:
            self.ticker_calls += 1
            return ft.RET_OK, pd.DataFrame([{"price": 303.0}])

    data = _Data()

    assert _session_price(data, "US.AAPL", "PRE") == 101.0
    assert _session_price(data, "US.AAPL", "AFTER") == 202.0
    assert _session_price(data, "US.AAPL", "RTH") == 999.0
    assert data.ticker_calls == 0


def test_forward_monitor_session_price_never_uses_rth_last_price_for_eth() -> None:
    class _Data:
        def get_market_snapshot(self, _code: str) -> tuple[int, pd.DataFrame]:
            return ft.RET_OK, pd.DataFrame(
                [{"pre_price": 0.0, "after_price": 0.0, "last_price": 999.0}]
            )

        def get_rt_ticker(self, _code: str, _num: int) -> tuple[int, pd.DataFrame]:
            return ft.RET_OK, pd.DataFrame()

    data = _Data()

    assert _session_price(data, "US.AAPL", "PRE") is None
    assert _session_price(data, "US.AAPL", "AFTER") is None
