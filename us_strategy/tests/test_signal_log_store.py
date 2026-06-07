# -*- coding: utf-8 -*-
"""SignalLogStore migration and session field tests."""

import json
import sqlite3
from pathlib import Path

from us_strategy.persistence import SignalLogStore


def test_signal_log_store_migrates_market_session(tmp_path: Path) -> None:
    db = tmp_path / "positions.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE signal_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                code TEXT NOT NULL,
                last_price REAL NOT NULL,
                scores TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO signal_log (ts, code, last_price, scores) VALUES (?,?,?,?)",
            (
                "2026-06-05T14:00:00+00:00",
                "US.AAPL",
                100.0,
                json.dumps({"turnover": 20.0}),
            ),
        )

    store = SignalLogStore(str(db))
    store.log("US.MSFT", 200.0, {"turnover": 30.0}, market_session="pre")
    records = store.load()

    assert records[0].market_session == "RTH"
    assert records[1].market_session == "PRE"

