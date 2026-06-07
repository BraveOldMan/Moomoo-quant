from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from research.cache import SQLiteQuoteContext
from research.panel import build_factor_panel
from research.signal_lab import _write_ic_outputs
from us_strategy import features
from us_strategy.config import StrategyConfig


def _seed_sqlite_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE history_kline (
                _code TEXT,
                time_key TEXT,
                open REAL,
                close REAL,
                high REAL,
                low REAL,
                turnover REAL,
                turnover_rate REAL,
                volume REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE capital_flow_day (
                _code TEXT,
                capital_flow_item_time TEXT,
                main_in_flow REAL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE daily_short_volume (
                _code TEXT,
                timestamp_str TEXT,
                short_percent REAL,
                daily_trade_avg_ratio REAL,
                total_shares_short REAL,
                volume REAL
            )
            """
        )
        for idx, close in enumerate((10.0, 10.5, 10.2, 10.8), start=1):
            day = f"2026-01-0{idx}"
            conn.execute(
                "INSERT INTO history_kline VALUES (?,?,?,?,?,?,?,?,?)",
                ("US.A", day, close, close, close + 0.2, close - 0.2, 2_000_000, 0.02, 1_000),
            )
            conn.execute(
                "INSERT INTO capital_flow_day VALUES (?,?,?)",
                ("US.A", day, 10_000.0),
            )
            conn.execute(
                "INSERT INTO daily_short_volume VALUES (?,?,?,?,?,?)",
                ("US.A", day, 5.0, 1.0, 1_000_000, 1_000_000),
            )
        conn.commit()
    finally:
        conn.close()


def test_sqlite_quote_context_reads_history_and_short_volume(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    _seed_sqlite_db(db_path)
    quote = SQLiteQuoteContext(db_path)

    ret, frame, _ = quote.request_history_kline("US.A", "2026-01-01", "2026-01-04")
    ret_short, short = quote.get_daily_short_volume(
        "US.A",
        start="2026-01-01",
        end="2026-01-04",
    )

    assert ret == 0
    assert ret_short == 0
    assert frame["close"].tolist() == [10.0, 10.5, 10.2, 10.8]
    assert short["short_percent"].tolist() == [5.0, 5.0, 5.0, 5.0]


def test_sqlite_factor_panel_includes_short_factor(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    _seed_sqlite_db(db_path)
    market = SimpleNamespace(config=StrategyConfig(), features=features)

    panel = build_factor_panel(
        SQLiteQuoteContext(db_path),
        market,
        ["US.A"],
        "2026-01-01",
        "2026-01-04",
        horizon_days=1,
    )

    assert not panel.empty
    assert "short" in panel.columns
    assert panel["short"].notna().all()


def test_ic_outputs_include_gate_status_for_short(tmp_path: Path) -> None:
    dates = pd.date_range("2026-01-01", periods=4, freq="D").strftime("%Y-%m-%d")
    rows = []
    base_returns = [0.08, 0.07, 0.06, 0.05, 0.04, 0.03, 0.02, 0.01]
    for day_index, day in enumerate(dates):
        returns = base_returns.copy()
        if day_index % 2:
            returns[2], returns[3] = returns[3], returns[2]
        if day_index % 3:
            returns[5] += 0.005
        for idx in range(8):
            rows.append(
                {
                    "date": day,
                    "capital": float(idx),
                    "turnover": float(idx),
                    "momentum": float(idx),
                    "short": float(idx),
                    "forward_return": returns[idx],
                }
            )
    panel = pd.DataFrame(rows)

    _write_ic_outputs(
        tmp_path,
        panel,
        min_days=2,
        ic_min=0.03,
        ir_min=0.5,
        min_pairs_per_day=4,
    )

    out = pd.read_csv(tmp_path / "ic_diagnostics.csv")
    short = out[out["factor"] == "short"].iloc[0]
    assert short["gate_status"] == "eligible"
    assert bool(short["gate_eligible"])
