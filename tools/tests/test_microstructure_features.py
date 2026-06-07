from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.microstructure_features import load_latest_features


def test_load_latest_features_uses_aggregate_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE microstructure_daily_features (
                trade_date TEXT,
                _code TEXT,
                market TEXT,
                dark_pool_event_count INTEGER,
                dark_pool_net_ratio REAL,
                dark_pool_score_max REAL,
                l2_snapshot_count INTEGER,
                l2_score_avg REAL,
                l2_score_max REAL,
                l2_imbalance_avg REAL,
                l2_danger_count INTEGER,
                broker_snapshot_count INTEGER,
                broker_score_avg REAL,
                broker_score_max REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE realtime_quote_snapshots (
                _code TEXT,
                trade_date TEXT,
                snapshot_ts_utc TEXT,
                last_price REAL,
                bid_price REAL,
                ask_price REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE order_book_metrics (
                _code TEXT,
                snapshot_ts_utc TEXT,
                spread_bps REAL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO microstructure_daily_features
            VALUES (
                '2026-06-05',
                'HK.00700',
                'HK',
                2,
                0.2,
                70.0,
                10,
                55.0,
                80.0,
                -0.1,
                1,
                4,
                60.0,
                90.0
            )
            """
        )
        conn.execute(
            """
            INSERT INTO realtime_quote_snapshots
            VALUES ('HK.00700', '2026-06-05', '2026-06-05T08:00:00Z', 400, 399, 401)
            """
        )
        conn.execute(
            """
            INSERT INTO order_book_metrics
            VALUES ('HK.00700', '2026-06-05T08:00:00Z', 4.5)
            """
        )
        conn.commit()
    finally:
        conn.close()

    rows = load_latest_features(db_path, ("HK.00700",))

    assert rows[0].trade_date == "2026-06-05"
    assert rows[0].last_price == 400.0
    assert rows[0].l2_snapshot_count == 10
    assert rows[0].broker_score_max == 90.0
    assert rows[0].spread_bps == 4.5
