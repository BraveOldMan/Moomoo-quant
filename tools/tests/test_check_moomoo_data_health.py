from __future__ import annotations

import sqlite3
from pathlib import Path

from tools.check_moomoo_data_health import (
    build_health_report,
    load_watchlist,
    render_markdown,
    write_report,
)


def test_load_watchlist_filters_codes(tmp_path: Path) -> None:
    path = tmp_path / "watchlist.txt"
    path.write_text("US.AAPL\nHK.00700\nUS.AAPL, US.MSFT\n", encoding="utf-8")

    assert load_watchlist(path, "US.") == ("US.AAPL", "US.MSFT")


def test_build_health_report_flags_missing_realtime_as_warn(tmp_path: Path) -> None:
    db_path = tmp_path / "history.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE history_kline (_code TEXT, market TEXT, time_key TEXT)")
        conn.execute(
            "CREATE TABLE market_snapshot (_code TEXT, market TEXT, snapshot_date TEXT)"
        )
        conn.execute(
            "CREATE TABLE realtime_quote_snapshots "
            "(_code TEXT, market TEXT, trade_date TEXT)"
        )
        conn.execute("CREATE TABLE realtime_ticks (_code TEXT)")
        conn.execute("CREATE TABLE order_book_metrics (_code TEXT)")
        conn.execute(
            "CREATE TABLE microstructure_daily_features "
            "(_code TEXT, market TEXT, trade_date TEXT)"
        )
        conn.execute(
            "INSERT INTO history_kline VALUES ('US.AAPL', 'US', '2026-06-05')"
        )
        conn.execute(
            "INSERT INTO market_snapshot VALUES ('US.AAPL', 'US', '2026-06-05')"
        )
        conn.commit()
    finally:
        conn.close()

    report = build_health_report(
        db_path,
        ("US",),
        ("US.AAPL",),
        (),
        target_date="2026-06-05",
    )

    assert report.overall_status == "warn"
    assert any(item.check == "history_kline" and item.status == "ok" for item in report.items)
    assert any(
        item.check == "realtime_quote_snapshots" and item.status == "warn"
        for item in report.items
    )
    assert "Moomoo Data Health" in render_markdown(report)


def test_write_report_creates_json_and_markdown(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"
    report = build_health_report(db_path, ("US",), ("US.AAPL",), ())

    json_path, md_path = write_report(report, tmp_path / "out")

    assert json_path.exists()
    assert md_path.exists()
