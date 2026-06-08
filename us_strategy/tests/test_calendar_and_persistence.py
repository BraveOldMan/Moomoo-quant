# -*- coding: utf-8 -*-
"""NYSE 假日表与 SQLite 持久化单测。"""

import sqlite3
from datetime import date

from us_strategy.market_calendar import get_nyse_holidays, is_trading_day
from us_strategy.persistence import PositionRecord, PositionStore


# ── 交易日历 ────────────────────────────────────────────────────────────
def test_independence_day_2024_is_holiday():
    assert date(2024, 7, 4) in get_nyse_holidays(2024)
    assert is_trading_day(date(2024, 7, 4)) is False


def test_christmas_2024_is_holiday():
    assert date(2024, 12, 25) in get_nyse_holidays(2024)


def test_weekend_is_not_trading_day():
    assert is_trading_day(date(2024, 1, 6)) is False  # 周六
    assert is_trading_day(date(2024, 1, 7)) is False  # 周日


def test_normal_weekday_is_trading_day():
    assert is_trading_day(date(2024, 1, 8)) is True  # 周一


def test_juneteenth_after_2022():
    assert date(2024, 6, 19) in get_nyse_holidays(2024)
    assert date(2020, 6, 19) not in get_nyse_holidays(2020)


def test_new_year_observed_on_weekend():
    # 2022-01-01 是周六 → 观察日提前到 2021-12-31 周五
    assert date(2021, 12, 31) in get_nyse_holidays(2022)


# ── 持久化 ──────────────────────────────────────────────────────────────
def test_position_store_roundtrip(tmp_path):
    db = str(tmp_path / "pos.db")
    store = PositionStore(db)
    rec = PositionRecord(
        code="US.RDDT",
        cost_price=45.5,
        buy_date=date(2024, 3, 21),
        tranches_bought=2,
        peak_price=50.0,
        qty=120,
        origin="ipo",
    )
    store.save(rec)
    loaded = store.load_all()
    assert "US.RDDT" in loaded
    got = loaded["US.RDDT"]
    assert got.cost_price == 45.5
    assert got.qty == 120
    assert got.tranches_bought == 2
    assert got.buy_date == date(2024, 3, 21)
    assert got.origin == "ipo"


def test_position_store_upsert(tmp_path):
    db = str(tmp_path / "pos.db")
    store = PositionStore(db)
    store.save(PositionRecord("US.X", 10, date(2024, 1, 2), 1, 10, 100))
    store.save(PositionRecord("US.X", 12, date(2024, 1, 2), 2, 14, 200))
    loaded = store.load_all()
    assert loaded["US.X"].cost_price == 12
    assert loaded["US.X"].qty == 200
    assert loaded["US.X"].origin == "regular"


def test_position_store_delete(tmp_path):
    db = str(tmp_path / "pos.db")
    store = PositionStore(db)
    store.save(PositionRecord("US.X", 10, date(2024, 1, 2), 1, 10, 100))
    store.delete("US.X")
    assert store.load_all() == {}


def test_position_store_migrates_missing_origin_as_regular(tmp_path):
    db = tmp_path / "old_pos.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE positions (
                code            TEXT PRIMARY KEY,
                cost_price      REAL NOT NULL,
                buy_date        TEXT NOT NULL,
                tranches_bought INTEGER NOT NULL DEFAULT 1,
                peak_price      REAL NOT NULL,
                qty             REAL NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            "INSERT INTO positions VALUES (?, ?, ?, ?, ?, ?)",
            ("US.OLD", 10.0, "2026-06-08", 1, 11.0, 100.0),
        )

    loaded = PositionStore(str(db)).load_all()

    assert loaded["US.OLD"].origin == "regular"
