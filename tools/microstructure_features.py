from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


DEFAULT_DB = "us_strategy/history_data.db"


@dataclass(frozen=True)
class MicrostructureFeature:
    """Aggregated local microstructure feature row for strategy/research use."""

    code: str
    market: str
    trade_date: str | None
    last_price: float | None
    bid_price: float | None
    ask_price: float | None
    quote_ts_utc: str | None
    spread_bps: float | None
    l2_snapshot_count: int
    l2_score_avg: float | None
    l2_score_max: float | None
    l2_imbalance_avg: float | None
    l2_danger_count: int
    dark_pool_event_count: int
    dark_pool_net_ratio: float | None
    dark_pool_score_max: float | None
    broker_snapshot_count: int
    broker_score_avg: float | None
    broker_score_max: float | None


def load_latest_features(
    db_path: Path,
    codes: tuple[str, ...],
    trade_date: str | None = None,
) -> list[MicrostructureFeature]:
    """Load aggregated microstructure features without scanning raw tick rows."""

    conn = sqlite3.connect(str(db_path))
    try:
        return [
            load_code_feature(conn, code, trade_date)
            for code in codes
        ]
    finally:
        conn.close()


def load_code_feature(
    conn: sqlite3.Connection,
    code: str,
    trade_date: str | None = None,
) -> MicrostructureFeature:
    """Load one code's latest local microstructure feature snapshot."""

    market = code.split(".", 1)[0] if "." in code else ""
    date_value = trade_date or _latest_code_date(conn, code)
    daily = _daily_feature_row(conn, code, date_value)
    quote = _latest_quote_row(conn, code, date_value)
    order_book = _latest_order_book_row(conn, code)
    return MicrostructureFeature(
        code=code,
        market=market,
        trade_date=date_value,
        last_price=_float_or_none(quote.get("last_price")),
        bid_price=_float_or_none(quote.get("bid_price")),
        ask_price=_float_or_none(quote.get("ask_price")),
        quote_ts_utc=_str_or_none(quote.get("snapshot_ts_utc")),
        spread_bps=_float_or_none(order_book.get("spread_bps")),
        l2_snapshot_count=int(daily.get("l2_snapshot_count") or 0),
        l2_score_avg=_float_or_none(daily.get("l2_score_avg")),
        l2_score_max=_float_or_none(daily.get("l2_score_max")),
        l2_imbalance_avg=_float_or_none(daily.get("l2_imbalance_avg")),
        l2_danger_count=int(daily.get("l2_danger_count") or 0),
        dark_pool_event_count=int(daily.get("dark_pool_event_count") or 0),
        dark_pool_net_ratio=_float_or_none(daily.get("dark_pool_net_ratio")),
        dark_pool_score_max=_float_or_none(daily.get("dark_pool_score_max")),
        broker_snapshot_count=int(daily.get("broker_snapshot_count") or 0),
        broker_score_avg=_float_or_none(daily.get("broker_score_avg")),
        broker_score_max=_float_or_none(daily.get("broker_score_max")),
    )


def main() -> int:
    """Print local aggregated microstructure features as JSON."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--codes", required=True)
    parser.add_argument("--date", default="")
    args = parser.parse_args()

    codes = tuple(code.strip() for code in args.codes.split(",") if code.strip())
    rows = load_latest_features(Path(args.db), codes, args.date or None)
    print(json.dumps([asdict(row) for row in rows], ensure_ascii=False, indent=2))
    return 0


def _latest_code_date(conn: sqlite3.Connection, code: str) -> str | None:
    candidates: list[str] = []
    for table, column in (
        ("microstructure_daily_features", "trade_date"),
        ("realtime_quote_snapshots", "trade_date"),
    ):
        if not _has_columns(conn, table, ("_code", column)):
            continue
        value = conn.execute(
            f"SELECT MAX(substr({column}, 1, 10)) FROM {table} WHERE _code=?",
            (code,),
        ).fetchone()[0]
        if value:
            candidates.append(str(value))
    return max(candidates) if candidates else None


def _daily_feature_row(
    conn: sqlite3.Connection,
    code: str,
    trade_date: str | None,
) -> dict[str, Any]:
    if not trade_date or not _has_columns(
        conn,
        "microstructure_daily_features",
        ("_code", "trade_date"),
    ):
        return {}
    row = conn.execute(
        """
        SELECT *
          FROM microstructure_daily_features
         WHERE _code = ? AND trade_date = ?
        """,
        (code, trade_date),
    ).fetchone()
    return _row_dict(conn, "microstructure_daily_features", row)


def _latest_quote_row(
    conn: sqlite3.Connection,
    code: str,
    trade_date: str | None,
) -> dict[str, Any]:
    if not _has_columns(
        conn,
        "realtime_quote_snapshots",
        ("_code", "trade_date", "snapshot_ts_utc"),
    ):
        return {}
    if trade_date:
        row = conn.execute(
            """
            SELECT *
              FROM realtime_quote_snapshots
             WHERE _code = ? AND trade_date = ?
             ORDER BY snapshot_ts_utc DESC
             LIMIT 1
            """,
            (code, trade_date),
        ).fetchone()
    else:
        row = conn.execute(
            """
            SELECT *
              FROM realtime_quote_snapshots
             WHERE _code = ?
             ORDER BY snapshot_ts_utc DESC
             LIMIT 1
            """,
            (code,),
        ).fetchone()
    return _row_dict(conn, "realtime_quote_snapshots", row)


def _latest_order_book_row(conn: sqlite3.Connection, code: str) -> dict[str, Any]:
    if not _has_columns(conn, "order_book_metrics", ("_code", "snapshot_ts_utc")):
        return {}
    row = conn.execute(
        """
        SELECT *
          FROM order_book_metrics
         WHERE _code = ?
         ORDER BY snapshot_ts_utc DESC
         LIMIT 1
        """,
        (code,),
    ).fetchone()
    return _row_dict(conn, "order_book_metrics", row)


def _row_dict(
    conn: sqlite3.Connection,
    table: str,
    row: sqlite3.Row | tuple[Any, ...] | None,
) -> dict[str, Any]:
    if row is None:
        return {}
    columns = [info[1] for info in conn.execute(f"PRAGMA table_info({table})")]
    return dict(zip(columns, row))


def _has_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if not exists:
        return False
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return all(column in existing for column in columns)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
