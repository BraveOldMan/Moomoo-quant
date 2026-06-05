# -*- coding: utf-8 -*-
"""HKEX 假日表与 SQLite 持久化单测。"""

from datetime import date

from hk_strategy.market_calendar import get_hkex_holidays, is_trading_day
from hk_strategy.persistence import PositionRecord, PositionStore


# ── 交易日历（仅断言高置信度的固定/已确认假日）──────────────────────────
def test_fixed_public_holidays_2025_are_holidays():
    # 固定公历假日，可靠
    for d in (
        date(2025, 1, 1),
        date(2025, 5, 1),
        date(2025, 7, 1),
        date(2025, 10, 1),
        date(2025, 12, 25),
    ):
        assert d in get_hkex_holidays(2025)
        assert is_trading_day(d) is False


def test_lunar_new_year_2025_is_holiday():
    # 2025 农历新年初一 = 1/29（已确认）
    assert date(2025, 1, 29) in get_hkex_holidays(2025)
    assert is_trading_day(date(2025, 1, 29)) is False


def test_weekend_is_not_trading_day():
    assert is_trading_day(date(2025, 1, 4)) is False  # 周六
    assert is_trading_day(date(2025, 1, 5)) is False  # 周日


def test_normal_weekday_is_trading_day():
    assert is_trading_day(date(2025, 1, 2)) is True  # 周四，非假日


def test_unknown_year_returns_empty_and_warns():
    # 表中无 2099 年 → 返回空集（仅按周末处理），不抛错
    assert get_hkex_holidays(2099) == frozenset()
    assert is_trading_day(date(2099, 1, 2)) is True  # 周五，非周末→当作交易日


# ── 持久化 ──────────────────────────────────────────────────────────────
def test_position_store_roundtrip(tmp_path):
    db = str(tmp_path / "pos.db")
    store = PositionStore(db)
    rec = PositionRecord(
        code="HK.00700",
        cost_price=45.5,
        buy_date=date(2024, 3, 21),
        tranches_bought=2,
        peak_price=50.0,
        qty=120,
    )
    store.save(rec)
    loaded = store.load_all()
    assert "HK.00700" in loaded
    got = loaded["HK.00700"]
    assert got.cost_price == 45.5
    assert got.qty == 120
    assert got.tranches_bought == 2
    assert got.buy_date == date(2024, 3, 21)


def test_position_store_upsert(tmp_path):
    db = str(tmp_path / "pos.db")
    store = PositionStore(db)
    store.save(PositionRecord("HK.X", 10, date(2024, 1, 2), 1, 10, 100))
    store.save(PositionRecord("HK.X", 12, date(2024, 1, 2), 2, 14, 200))
    loaded = store.load_all()
    assert loaded["HK.X"].cost_price == 12
    assert loaded["HK.X"].qty == 200


def test_position_store_delete(tmp_path):
    db = str(tmp_path / "pos.db")
    store = PositionStore(db)
    store.save(PositionRecord("HK.X", 10, date(2024, 1, 2), 1, 10, 100))
    store.delete("HK.X")
    assert store.load_all() == {}
