from __future__ import annotations

import sqlite3
from datetime import date, time
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import moomoo as ft
import pandas as pd

from tools.backfill_moomoo_us_history import HistoryStore
from tools.daily_moomoo_watchlist_backfill import (
    MarketJob,
    MarketSpec,
    build_jobs,
    fetch_market_snapshots,
    hk_status_snapshot_record,
)


def test_hk_status_snapshot_record_keeps_status_fields() -> None:
    record = {
        "code": "HK.00700",
        "name": "Tencent",
        "last_price": 400.0,
        "dark_status": "TRADING",
        "sec_status": "NORMAL",
        "ignored": "value",
    }

    row = hk_status_snapshot_record(
        record,
        market="HK",
        snapshot_date="2026-06-05",
        snapshot_kind="after_close",
    )

    assert row["market"] == "HK"
    assert row["snapshot_date"] == "2026-06-05"
    assert row["snapshot_kind"] == "after_close"
    assert row["code"] == "HK.00700"
    assert row["dark_status"] == "TRADING"
    assert row["sec_status"] == "NORMAL"
    assert "ignored" not in row


def test_fetch_market_snapshots_stores_hk_status_table(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    store = HistoryStore(db_path, "run-1")
    job = MarketJob(
        spec=MarketSpec(
            label="HK",
            prefix="HK.",
            watchlist_path=tmp_path / "watchlist.txt",
            timezone=ZoneInfo("Asia/Hong_Kong"),
            close_time=time(16, 0),
            trade_date_market=ft.TradeDateMarket.HK,
            is_trading_day=lambda _day: True,
        ),
        target_date=date(2026, 6, 5),
        codes=("HK.00700",),
    )

    fetch_market_snapshots(_FakeApi(), store, job, batch_size=10)
    store.close()

    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM market_snapshot").fetchone()[0] == 1
        row = conn.execute(
            """
            SELECT code, dark_status, sec_status, snapshot_date, snapshot_kind
              FROM hk_market_status_snapshots
             WHERE _code = 'HK.00700'
            """
        ).fetchone()
        assert row == (
            "HK.00700",
            "TRADING",
            "NORMAL",
            "2026-06-05",
            "after_close",
        )
    finally:
        conn.close()


def test_build_jobs_merges_us_proxy_watchlist(tmp_path: Path) -> None:
    us_watchlist = tmp_path / "us_watchlist.txt"
    proxy_watchlist = tmp_path / "proxy_watchlist.txt"
    hk_watchlist = tmp_path / "hk_watchlist.txt"
    us_watchlist.write_text("US.AAPL\nUS.QQQ\n", encoding="utf-8")
    proxy_watchlist.write_text("US.SPY\nUS.QQQ\nUS.IBIT\n", encoding="utf-8")
    hk_watchlist.write_text("", encoding="utf-8")
    args = type(
        "Args",
        (),
        {
            "watchlist": "",
            "us_watchlist": str(us_watchlist),
            "us_proxy_watchlist": str(proxy_watchlist),
            "hk_watchlist": str(hk_watchlist),
            "markets": "US",
            "codes": "",
            "date": "2026-06-05",
            "after_close_delay_min": 90,
            "force": False,
        },
    )()

    jobs = build_jobs(args)

    assert len(jobs) == 1
    assert jobs[0].codes == ("US.AAPL", "US.QQQ", "US.SPY", "US.IBIT")


class _FakeApi:
    def __init__(self) -> None:
        self.ctx = self

    def call(
        self,
        fn: Callable[[], tuple[int, pd.DataFrame]],
    ) -> tuple[int, pd.DataFrame]:
        return fn()

    def get_market_snapshot(self, codes: list[str]) -> tuple[int, pd.DataFrame]:
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "code": codes[0],
                    "name": "Tencent",
                    "last_price": 400.0,
                    "turnover": 1_000_000.0,
                    "turnover_rate": 0.5,
                    "dark_status": "TRADING",
                    "sec_status": "NORMAL",
                    "update_time": "2026-06-05 16:00:00",
                }
            ]
        )
