# -*- coding: utf-8 -*-
"""港股交易时段（含午休）判定单测。"""

from datetime import datetime
from zoneinfo import ZoneInfo

from hk_strategy.config import StrategyConfig
from hk_strategy.main import _is_market_open

_HKT = ZoneInfo("Asia/Hong_Kong")


def _at(h: int, m: int, day: int = 2) -> datetime:
    # 2025-01-02 是周四、非假日
    return datetime(2025, 1, day, h, m, tzinfo=_HKT)


def _cfg() -> StrategyConfig:
    return StrategyConfig()  # 默认即港股时段


def test_morning_session_open():
    assert _is_market_open(_cfg(), now=_at(11, 0)) is True


def test_lunch_break_is_closed():
    assert _is_market_open(_cfg(), now=_at(12, 30)) is False


def test_afternoon_session_open():
    assert _is_market_open(_cfg(), now=_at(14, 0)) is True


def test_before_open_is_closed():
    assert _is_market_open(_cfg(), now=_at(9, 0)) is False


def test_after_close_is_closed():
    assert _is_market_open(_cfg(), now=_at(16, 30)) is False


def test_exact_open_is_open():
    assert _is_market_open(_cfg(), now=_at(9, 30)) is True


def test_morning_close_boundary_is_closed():
    # 12:00 起午休（右开区间）
    assert _is_market_open(_cfg(), now=_at(12, 0)) is False


def test_afternoon_open_boundary_is_open():
    assert _is_market_open(_cfg(), now=_at(13, 0)) is True


def test_market_close_boundary_is_closed():
    # 16:00 收盘（右开区间）
    assert _is_market_open(_cfg(), now=_at(16, 0)) is False


def test_weekend_is_closed():
    # 2025-01-04 周六
    assert _is_market_open(_cfg(), now=_at(11, 0, day=4)) is False


def test_holiday_is_closed():
    # 2025-01-01 元旦（周三）
    assert _is_market_open(_cfg(), now=_at(11, 0, day=1)) is False
