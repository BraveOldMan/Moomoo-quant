from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from hk_strategy import features
from hk_strategy.config import StrategyConfig
from research.cache import SQLiteQuoteContext
from research.panel import build_factor_panel


def test_sqlite_panel_includes_short_and_microstructure_scores(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            CREATE TABLE history_kline (
                time_key TEXT,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                turnover REAL,
                turnover_rate REAL,
                volume REAL,
                _code TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE capital_flow_day (
                capital_flow_item_time TEXT,
                main_in_flow REAL,
                _code TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE daily_short_volume (
                timestamp_str TEXT,
                short_percent REAL,
                daily_trade_avg_ratio REAL,
                total_shares_short REAL,
                volume REAL,
                _code TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE microstructure_daily_features (
                trade_date TEXT,
                _code TEXT,
                l2_score_avg REAL,
                l2_score_max REAL,
                l2_imbalance_avg REAL,
                l2_danger_count INTEGER,
                dark_pool_event_count INTEGER,
                dark_pool_net_ratio REAL,
                dark_pool_score_max REAL,
                broker_snapshot_count INTEGER,
                broker_score_avg REAL,
                broker_score_max REAL
            )
            """
        )
        for index, close in enumerate((10.0, 10.5, 11.0), start=2):
            day = f"2024-01-0{index}"
            conn.execute(
                """
                INSERT INTO history_kline
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (day, close, close, close + 0.1, close - 0.1, 1_000_000, 0.02, 1_000, "HK.TEST"),
            )
            conn.execute(
                "INSERT INTO capital_flow_day VALUES (?, ?, ?)",
                (day, 10_000.0, "HK.TEST"),
            )
            conn.execute(
                "INSERT INTO daily_short_volume VALUES (?, ?, ?, ?, ?, ?)",
                (day, 15.0, 0.0, 0.0, 1_000.0, "HK.TEST"),
            )
            conn.execute(
                """
                INSERT INTO microstructure_daily_features
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (day, "HK.TEST", 60.0, 80.0, -0.2, 1, 2, -0.5, 75.0, 3, 65.0, 90.0),
            )
        conn.commit()
    finally:
        conn.close()

    market = SimpleNamespace(config=StrategyConfig(), features=features)
    panel = build_factor_panel(
        SQLiteQuoteContext(db_path),
        market,
        ["HK.TEST"],
        "2024-01-02",
        "2024-01-04",
        horizon_days=1,
    )

    assert not panel.empty
    first = panel.iloc[0]
    assert first["short"] == 50.0
    assert first["l2_imbalance"] == 60.0
    assert first["dark_pool_proxy"] == 75.0
    assert first["broker"] == 65.0
