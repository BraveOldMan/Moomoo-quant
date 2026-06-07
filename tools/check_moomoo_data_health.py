from __future__ import annotations

import argparse
import json
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_DB = "us_strategy/history_data.db"
DEFAULT_OUTPUT_DIR = "report/outputs/data_health"
DEFAULT_US_WATCHLIST = "us_strategy/watchlist.txt"
DEFAULT_HK_WATCHLIST = "hk_strategy/watchlist.txt"


@dataclass(frozen=True)
class HealthItem:
    """One local database health check result."""

    market: str
    code: str
    check: str
    status: str
    trade_date: str | None
    observed: int
    message: str


@dataclass(frozen=True)
class HealthReport:
    """Full health report for local Moomoo market data."""

    db_path: str
    overall_status: str
    items: list[HealthItem]


def load_watchlist(path: Path, prefix: str) -> tuple[str, ...]:
    """Load a watchlist file and keep codes matching the market prefix."""

    if not path.exists():
        return ()
    seen: set[str] = set()
    codes: list[str] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            clean = line.split("#", 1)[0].strip()
            if not clean:
                continue
            for raw in clean.split(","):
                code = raw.strip()
                if code.startswith(prefix) and code not in seen:
                    seen.add(code)
                    codes.append(code)
    return tuple(codes)


def build_health_report(
    db_path: Path,
    markets: tuple[str, ...],
    us_codes: tuple[str, ...],
    hk_codes: tuple[str, ...],
    target_date: str | None = None,
) -> HealthReport:
    """Build a read-only health report from the local market data database."""

    if not db_path.exists():
        return HealthReport(
            db_path=str(db_path),
            overall_status="fail",
            items=[
                HealthItem(
                    market="ALL",
                    code="",
                    check="database",
                    status="fail",
                    trade_date=target_date,
                    observed=0,
                    message="database file is missing",
                )
            ],
        )

    conn = sqlite3.connect(str(db_path))
    try:
        items: list[HealthItem] = []
        for market in markets:
            codes = us_codes if market == "US" else hk_codes
            prefix = f"{market}."
            date_value = target_date or latest_market_date(conn, market)
            if not codes:
                items.append(
                    HealthItem(
                        market=market,
                        code="",
                        check="watchlist",
                        status="warn",
                        trade_date=date_value,
                        observed=0,
                        message=f"no {market} watchlist codes",
                    )
                )
                continue
            if not date_value:
                items.append(
                    HealthItem(
                        market=market,
                        code="",
                        check="latest_trade_date",
                        status="fail",
                        trade_date=None,
                        observed=0,
                        message=f"no local {market} trade date evidence",
                    )
                )
                continue
            for code in codes:
                if not code.startswith(prefix):
                    items.append(
                        HealthItem(
                            market=market,
                            code=code,
                            check="code_prefix",
                            status="fail",
                            trade_date=date_value,
                            observed=0,
                            message=f"code does not start with {prefix}",
                        )
                    )
                    continue
                items.extend(_code_checks(conn, market, code, date_value))
        return HealthReport(
            db_path=str(db_path),
            overall_status=_overall_status(items),
            items=items,
        )
    finally:
        conn.close()


def latest_market_date(conn: sqlite3.Connection, market: str) -> str | None:
    """Return the latest local trade date seen for one market."""

    candidates: list[str] = []
    for table, column, where in (
        ("history_kline", "time_key", "market = ?"),
        ("market_snapshot", "snapshot_date", "market = ?"),
        ("realtime_quote_snapshots", "trade_date", "market = ?"),
        ("microstructure_daily_features", "trade_date", "market = ?"),
    ):
        if not _has_columns(conn, table, (column, "market")):
            continue
        value = conn.execute(
            f"SELECT MAX(substr({column}, 1, 10)) FROM {table} WHERE {where}",
            (market,),
        ).fetchone()[0]
        if value:
            candidates.append(str(value))
    return max(candidates) if candidates else None


def write_report(report: HealthReport, output_dir: Path) -> tuple[Path, Path]:
    """Write JSON and Markdown health report files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "data_health.json"
    md_path = output_dir / "data_health.md"
    json_path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def render_markdown(report: HealthReport) -> str:
    """Render a compact Markdown data health report."""

    lines = [
        "# Moomoo Data Health",
        "",
        f"- database: `{report.db_path}`",
        f"- overall_status: `{report.overall_status}`",
        "",
        "| market | code | check | date | status | observed | message |",
        "|---|---|---|---|---|---:|---|",
    ]
    for item in report.items:
        lines.append(
            f"| {item.market} | `{item.code}` | {item.check} | "
            f"{item.trade_date or ''} | {item.status} | {item.observed} | "
            f"{item.message} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    """Check local US/HK historical and realtime data coverage."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--markets", default="US,HK")
    parser.add_argument("--us-watchlist", default=DEFAULT_US_WATCHLIST)
    parser.add_argument("--hk-watchlist", default=DEFAULT_HK_WATCHLIST)
    parser.add_argument("--date", default="")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    markets = tuple(
        market.strip().upper()
        for market in args.markets.split(",")
        if market.strip()
    )
    report = build_health_report(
        Path(args.db),
        markets,
        load_watchlist(Path(args.us_watchlist), "US."),
        load_watchlist(Path(args.hk_watchlist), "HK."),
        args.date or None,
    )
    json_path, md_path = write_report(report, Path(args.output_dir))
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(render_markdown(report), end="")
        print(f"wrote {json_path} and {md_path}")
    if args.strict and report.overall_status != "ok":
        return 1
    return 0


def _code_checks(
    conn: sqlite3.Connection,
    market: str,
    code: str,
    trade_date: str,
) -> list[HealthItem]:
    checks = [
        _count_check(
            conn,
            market,
            code,
            trade_date,
            "history_kline",
            "history_kline",
            "fail",
            "daily kline rows",
        ),
        _count_check(
            conn,
            market,
            code,
            trade_date,
            "market_snapshot",
            "market_snapshot",
            "fail",
            "after-close snapshot rows",
        ),
        _count_check(
            conn,
            market,
            code,
            trade_date,
            "realtime_quote_snapshots",
            "realtime_quote_snapshots",
            "warn",
            "low-frequency realtime quote snapshots",
        ),
        _count_check(
            conn,
            market,
            code,
            None,
            "realtime_ticks",
            "realtime_ticks",
            "warn",
            "realtime tick rows",
        ),
        _count_check(
            conn,
            market,
            code,
            None,
            "order_book_metrics",
            "order_book_metrics",
            "warn",
            "L2 order book metric rows",
        ),
        _count_check(
            conn,
            market,
            code,
            trade_date,
            "microstructure_daily_features",
            "microstructure_daily_features",
            "warn",
            "daily microstructure feature row",
        ),
    ]
    if market == "HK":
        checks.append(
            _count_check(
                conn,
                market,
                code,
                trade_date,
                "broker_queue_metrics",
                "broker_queue_metrics",
                "warn",
                "HK broker queue metrics",
            )
        )
    checks.extend(_run_checks(conn, market, trade_date))
    return checks


def _count_check(
    conn: sqlite3.Connection,
    market: str,
    code: str,
    trade_date: str | None,
    table: str,
    check: str,
    missing_status: str,
    label: str,
) -> HealthItem:
    if not _table_exists(conn, table):
        return HealthItem(
            market=market,
            code=code,
            check=check,
            status=missing_status,
            trade_date=trade_date,
            observed=0,
            message=f"{table} table is missing",
        )
    if table in {"history_kline", "market_snapshot"}:
        date_column = "time_key" if table == "history_kline" else "snapshot_date"
        observed = _count_code_date(conn, table, code, trade_date, date_column)
    elif table in {
        "realtime_quote_snapshots",
        "microstructure_daily_features",
        "broker_queue_metrics",
    }:
        observed = _count_code_date(conn, table, code, trade_date, "trade_date")
    else:
        observed = _count_code(conn, table, code)
    return HealthItem(
        market=market,
        code=code,
        check=check,
        status="ok" if observed > 0 else missing_status,
        trade_date=trade_date,
        observed=observed,
        message=f"{label}: {observed}",
    )


def _run_checks(
    conn: sqlite3.Connection,
    market: str,
    trade_date: str,
) -> list[HealthItem]:
    items: list[HealthItem] = []
    if _has_columns(conn, "backfill_runs", ("status", "finished_at")):
        status = _latest_status(conn, "backfill_runs")
        items.append(
            HealthItem(
                market=market,
                code="",
                check="latest_backfill_run",
                status="fail" if status == "failed" else "ok",
                trade_date=trade_date,
                observed=1 if status else 0,
                message=f"latest backfill status: {status or 'missing'}",
            )
        )
    if _has_columns(conn, "tick_runs", ("status", "finished_at")):
        status = _latest_status(conn, "tick_runs")
        items.append(
            HealthItem(
                market=market,
                code="",
                check="latest_tick_run",
                status="fail" if status == "failed" else "ok",
                trade_date=trade_date,
                observed=1 if status else 0,
                message=f"latest tick status: {status or 'missing'}",
            )
        )
    return items


def _latest_status(conn: sqlite3.Connection, table: str) -> str | None:
    row = conn.execute(
        f"SELECT status FROM {table} ORDER BY COALESCE(finished_at, started_at) DESC LIMIT 1"
    ).fetchone()
    return str(row[0]) if row else None


def _overall_status(items: list[HealthItem]) -> str:
    statuses = {item.status for item in items}
    if "fail" in statuses:
        return "fail"
    if "warn" in statuses:
        return "warn"
    return "ok"


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return row is not None


def _has_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
) -> bool:
    if not _table_exists(conn, table):
        return False
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    return all(column in existing for column in columns)


def _count_code(
    conn: sqlite3.Connection,
    table: str,
    code: str,
) -> int:
    if not _has_columns(conn, table, ("_code",)):
        return 0
    return int(
        conn.execute(f"SELECT COUNT(*) FROM {table} WHERE _code=?", (code,)).fetchone()[
            0
        ]
    )


def _count_code_date(
    conn: sqlite3.Connection,
    table: str,
    code: str,
    trade_date: str | None,
    date_column: str,
) -> int:
    if not trade_date or not _has_columns(conn, table, ("_code", date_column)):
        return 0
    return int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            f"WHERE _code=? AND substr({date_column}, 1, 10)=?",
            (code, trade_date),
        ).fetchone()[0]
    )


if __name__ == "__main__":
    raise SystemExit(main())
