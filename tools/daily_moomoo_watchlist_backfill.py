from __future__ import annotations

import argparse
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import moomoo as ft
import pandas as pd

from hk_strategy.market_calendar import is_trading_day as is_hk_trading_day
from moomoo_rate_limits import DEFAULT_BACKFILL_SLEEP_SECONDS
from tools.backfill_moomoo_us_history import (
    DEFAULT_DB,
    ApiRunner,
    BackfillConfig,
    HistoryStore,
    fetch_quota,
    ok_or_error,
)
from us_strategy.market_calendar import is_trading_day as is_us_trading_day


DEFAULT_HISTORY_START = "2024-01-01"
DEFAULT_US_WATCHLIST = "us_strategy/watchlist.txt"
DEFAULT_US_PROXY_WATCHLIST = "us_strategy/proxy_watchlist.txt"
DEFAULT_HK_WATCHLIST = "hk_strategy/watchlist.txt"
HK_STATUS_FIELDS = (
    "code",
    "name",
    "last_price",
    "cur_price",
    "turnover",
    "turnover_rate",
    "lot_size",
    "listing_date",
    "price_spread",
    "dark_status",
    "sec_status",
    "update_time",
)


@dataclass(frozen=True)
class MarketSpec:
    """Market-specific settings for the daily after-close backfill."""

    label: str
    prefix: str
    watchlist_path: Path
    timezone: ZoneInfo
    close_time: time
    trade_date_market: str
    is_trading_day: Callable[[date], bool]


@dataclass(frozen=True)
class MarketJob:
    """One market/date/codes unit to fetch in a single daily run."""

    spec: MarketSpec
    target_date: date
    codes: tuple[str, ...]


def parse_args() -> argparse.Namespace:
    """Parse dual-market daily watchlist backfill options."""

    parser = argparse.ArgumentParser(
        description=(
            "Backfill US/HK watchlist daily historical K-line data and "
            "after-close market snapshots into the local SQLite database."
        ),
    )
    parser.add_argument("--codes", default="", help="Comma-separated US./HK. symbols.")
    parser.add_argument(
        "--markets",
        default="US,HK",
        help="Comma-separated markets to run, default: US,HK.",
    )
    parser.add_argument("--us-watchlist", default=DEFAULT_US_WATCHLIST)
    parser.add_argument("--us-proxy-watchlist", default=DEFAULT_US_PROXY_WATCHLIST)
    parser.add_argument("--hk-watchlist", default=DEFAULT_HK_WATCHLIST)
    parser.add_argument(
        "--watchlist",
        default="",
        help="Backward-compatible alias for --us-watchlist.",
    )
    parser.add_argument(
        "--history-start",
        default=DEFAULT_HISTORY_START,
        help="Start date for daily historical K-line backfill.",
    )
    parser.add_argument(
        "--date",
        default="",
        help="Explicit market date for all selected markets, YYYY-MM-DD.",
    )
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--sleep", type=float, default=DEFAULT_BACKFILL_SLEEP_SECONDS)
    parser.add_argument("--snapshot-batch-size", type=int, default=200)
    parser.add_argument("--after-close-delay-min", type=int, default=90)
    parser.add_argument(
        "--only-snapshot",
        action="store_true",
        help="Only fetch get_market_snapshot rows; skip history K-line backfill.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Use each market's local today even if the close delay has not elapsed.",
    )
    return parser.parse_args()


def load_watchlist(path: Path, prefix: str) -> tuple[str, ...]:
    """Load one market watchlist and keep only codes with the expected prefix."""

    if not path.exists():
        return ()
    raw_codes: list[str] = []
    with path.open("r", encoding="utf-8-sig") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            raw_codes.extend(part.strip() for part in line.split(","))

    seen: set[str] = set()
    codes: list[str] = []
    for code in raw_codes:
        if code.startswith(prefix) and code not in seen:
            seen.add(code)
            codes.append(code)
    return tuple(codes)


def parse_explicit_codes(raw: str) -> dict[str, tuple[str, ...]]:
    """Group explicit CLI codes by market prefix."""

    grouped: dict[str, list[str]] = {"US": [], "HK": []}
    seen: set[str] = set()
    for item in raw.split(","):
        code = item.strip()
        if not code or code in seen:
            continue
        seen.add(code)
        if code.startswith("US."):
            grouped["US"].append(code)
        elif code.startswith("HK."):
            grouped["HK"].append(code)
    return {market: tuple(codes) for market, codes in grouped.items()}


def parse_markets(raw: str) -> tuple[str, ...]:
    """Parse and validate selected market labels."""

    markets = tuple(
        market.strip().upper()
        for market in raw.split(",")
        if market.strip()
    )
    unknown = sorted(set(markets) - {"US", "HK"})
    if unknown:
        raise ValueError(f"unsupported markets: {','.join(unknown)}")
    if not markets:
        raise ValueError("no markets selected")
    return markets


def merge_codes(*groups: tuple[str, ...]) -> tuple[str, ...]:
    """Merge symbol groups while preserving order and removing duplicates."""

    seen: set[str] = set()
    out: list[str] = []
    for group in groups:
        for code in group:
            if code not in seen:
                seen.add(code)
                out.append(code)
    return tuple(out)


def build_market_specs(args: argparse.Namespace) -> dict[str, MarketSpec]:
    """Build US and HK market metadata."""

    us_watchlist = Path(args.watchlist or args.us_watchlist)
    return {
        "US": MarketSpec(
            label="US",
            prefix="US.",
            watchlist_path=us_watchlist,
            timezone=ZoneInfo("America/New_York"),
            close_time=time(16, 0),
            trade_date_market=ft.TradeDateMarket.US,
            is_trading_day=is_us_trading_day,
        ),
        "HK": MarketSpec(
            label="HK",
            prefix="HK.",
            watchlist_path=Path(args.hk_watchlist),
            timezone=ZoneInfo("Asia/Hong_Kong"),
            close_time=time(16, 0),
            trade_date_market=ft.TradeDateMarket.HK,
            is_trading_day=is_hk_trading_day,
        ),
    }


def infer_target_date(
    spec: MarketSpec,
    explicit_date: str,
    delay_minutes: int,
    force: bool,
) -> date:
    """Infer the latest local market date whose close should be available."""

    if explicit_date.strip():
        return date.fromisoformat(explicit_date)
    now = datetime.now(spec.timezone)
    if force:
        return now.date()
    ready_at = datetime.combine(
        now.date(),
        spec.close_time,
        tzinfo=spec.timezone,
    ) + timedelta(minutes=delay_minutes)
    if now >= ready_at:
        return now.date()
    return now.date() - timedelta(days=1)


def build_jobs(args: argparse.Namespace) -> tuple[MarketJob, ...]:
    """Resolve selected watchlists into per-market fetch jobs."""

    specs = build_market_specs(args)
    selected = parse_markets(args.markets)
    explicit_codes = parse_explicit_codes(args.codes)
    jobs: list[MarketJob] = []
    for market in selected:
        spec = specs[market]
        if args.codes.strip():
            codes = explicit_codes[market]
        else:
            codes = load_watchlist(spec.watchlist_path, spec.prefix)
            if market == "US":
                codes = merge_codes(
                    codes,
                    load_watchlist(Path(args.us_proxy_watchlist), spec.prefix),
                )
        if not codes:
            continue
        target_date = infer_target_date(
            spec,
            args.date,
            args.after_close_delay_min,
            args.force,
        )
        jobs.append(MarketJob(spec=spec, target_date=target_date, codes=codes))
    if not jobs:
        raise ValueError("no selected watchlist symbols found")
    return tuple(jobs)


def build_config(
    args: argparse.Namespace,
    jobs: tuple[MarketJob, ...],
) -> BackfillConfig:
    """Build the shared run header config used by the SQLite sink."""

    codes = tuple(code for job in jobs for code in job.codes)
    end = max(job.target_date for job in jobs).isoformat()
    return BackfillConfig(
        codes=codes,
        start=args.history_start,
        end=end,
        db_path=Path(args.db),
        host=args.host,
        port=args.port,
        sleep_seconds=args.sleep,
        page_size=1000,
        max_pages=200,
        option_contracts_per_expiry=0,
        max_option_contracts_per_code=0,
        option_chain_sleep_seconds=0.0,
        option_history_sleep_seconds=0.0,
        refresh_existing_options=False,
        include_fundamentals=False,
        only_options=False,
    )


def chunked(items: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    """Yield fixed-size chunks while preserving symbol order."""

    if size <= 0:
        raise ValueError("snapshot batch size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


def hk_status_snapshot_record(
    record: dict,
    *,
    market: str,
    snapshot_date: str,
    snapshot_kind: str,
) -> dict:
    """Return the structured HK status subset from a market snapshot row."""

    out = {
        "market": market,
        "snapshot_date": snapshot_date,
        "snapshot_kind": snapshot_kind,
    }
    for field in HK_STATUS_FIELDS:
        out[field] = record.get(field)
    return out


def fetch_trading_days(
    api: ApiRunner,
    store: HistoryStore,
    job: MarketJob,
    start: str,
) -> None:
    """Fetch and store one market trading calendar for the backfill window."""

    end = job.target_date.isoformat()
    ret, data = api.call(
        lambda: api.ctx.request_trading_days(
            market=job.spec.trade_date_market,
            start=start,
            end=end,
        )
    )
    ok, msg = ok_or_error(ret, data)
    if not ok:
        store.log_error(job.spec.label, "trading_days", "request_trading_days", msg)
        return
    records = data if isinstance(data, list) else []
    rows = [dict(item, market=job.spec.label) for item in records]
    count = store.store_records(
        "trading_days",
        job.spec.label,
        rows,
        key_fields=("market", "time", "trade_date_type"),
    )
    print(f"[{job.spec.label}:calendar] rows={count}", flush=True)


def fetch_history_kline(
    api: ApiRunner,
    store: HistoryStore,
    job: MarketJob,
    code: str,
    start: str,
) -> None:
    """Fetch adjusted daily K-line history for one watchlist symbol."""

    page_key: bytes | None = None
    total = 0
    pages = 0
    end = job.target_date.isoformat()
    while True:
        ret, data, next_key = api.call(
            lambda key=page_key: api.ctx.request_history_kline(
                code,
                start=start,
                end=end,
                ktype=ft.KLType.K_DAY,
                autype=ft.AuType.QFQ,
                max_count=1000,
                page_req_key=key,
                session=ft.Session.NONE,
            )
        )
        ok, msg = ok_or_error(ret, data)
        if not ok:
            store.log_error(code, "history_kline_day", "request_history_kline", msg)
            break
        count = store.store_frame(
            "history_kline",
            code,
            data,
            key_fields=("ktype", "autype", "session", "extended_time", "time_key"),
            extra={
                "market": job.spec.label,
                "ktype": ft.KLType.K_DAY,
                "autype": ft.AuType.QFQ,
                "session": ft.Session.NONE,
                "extended_time": False,
            },
        )
        total += count
        pages += 1
        if not next_key or pages >= 200:
            break
        page_key = next_key
    print(
        f"[{code}] history_kline_day rows={total} pages={pages} end={end}",
        flush=True,
    )


def fetch_market_snapshots(
    api: ApiRunner,
    store: HistoryStore,
    job: MarketJob,
    batch_size: int,
) -> None:
    """Fetch after-close market snapshots and upsert one row per code/date."""

    total = 0
    snapshot_date = job.target_date.isoformat()
    for batch in chunked(job.codes, batch_size):
        ret, data = api.call(
            lambda batch=batch: api.ctx.get_market_snapshot(list(batch)),
        )
        ok, msg = ok_or_error(ret, data)
        if not ok:
            for code in batch:
                store.log_error(
                    code,
                    "market_snapshot",
                    "get_market_snapshot",
                    msg,
                )
            continue
        if not isinstance(data, pd.DataFrame) or data.empty:
            for code in batch:
                store.add_stat(code, "market_snapshot", 0, "after_close")
            continue

        seen: set[str] = set()
        records = data.where(pd.notnull(data), None).to_dict("records")
        for record in records:
            code = str(record.get("code", ""))
            if not code:
                continue
            seen.add(code)
            total += store.store_records(
                "market_snapshot",
                code,
                [record],
                key_fields=("market", "snapshot_date", "snapshot_kind", "code"),
                extra={
                    "market": job.spec.label,
                    "snapshot_date": snapshot_date,
                    "snapshot_kind": "after_close",
                },
                note="after_close",
            )
            if job.spec.label == "HK":
                store.store_records(
                    "hk_market_status_snapshots",
                    code,
                    [
                        hk_status_snapshot_record(
                            record,
                            market=job.spec.label,
                            snapshot_date=snapshot_date,
                            snapshot_kind="after_close",
                        )
                    ],
                    key_fields=("market", "snapshot_date", "snapshot_kind", "code"),
                    note="after_close",
                )

        missing = set(batch) - seen
        for code in sorted(missing):
            store.add_stat(code, "market_snapshot", 0, "after_close_missing")

    print(
        f"[{job.spec.label}:market_snapshot] rows={total} date={snapshot_date}",
        flush=True,
    )


def active_jobs(store: HistoryStore, jobs: tuple[MarketJob, ...]) -> tuple[MarketJob, ...]:
    """Filter jobs to markets that traded on their target date."""

    active: list[MarketJob] = []
    for job in jobs:
        if job.spec.is_trading_day(job.target_date):
            active.append(job)
            continue
        note = f"not a {job.spec.label} trading day: {job.target_date.isoformat()}"
        store.add_stat(job.spec.label, "market_job", 0, note)
        print(f"[{job.spec.label}] skipped {note}", flush=True)
    return tuple(active)


def run_backfill(
    api: ApiRunner,
    store: HistoryStore,
    jobs: tuple[MarketJob, ...],
    history_start: str,
    snapshot_batch_size: int,
    only_snapshot: bool,
) -> None:
    """Run all selected market jobs through one OpenD quote connection."""

    if not only_snapshot:
        fetch_quota(api, store, "before_daily")
    for job in jobs:
        end = job.target_date.isoformat()
        print(
            f"[{job.spec.label}] date={end} codes={len(job.codes)} "
            f"history_start={history_start} only_snapshot={only_snapshot}",
            flush=True,
        )
        if not only_snapshot:
            fetch_trading_days(api, store, job, history_start)
            for code in job.codes:
                fetch_history_kline(api, store, job, code, history_start)
        fetch_market_snapshots(api, store, job, snapshot_batch_size)
    if not only_snapshot:
        fetch_quota(api, store, "after_daily")


def run_summary(jobs: tuple[MarketJob, ...]) -> str:
    """Return a compact JSON run summary for logs and DB notes."""

    return json.dumps(
        {
            job.spec.label: {
                "date": job.target_date.isoformat(),
                "codes": len(job.codes),
            }
            for job in jobs
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def main() -> int:
    """Run the daily dual-market watchlist data backfill."""

    args = parse_args()
    jobs = build_jobs(args)
    cfg = build_config(args, jobs)
    run_id = uuid.uuid4().hex
    store = HistoryStore(cfg.db_path, run_id)
    status = "success"
    note = None
    started = False
    try:
        store.start_run(cfg)
        started = True
        jobs_to_run = active_jobs(store, jobs)
        if not jobs_to_run:
            status = "skipped"
            note = "no selected market traded on target date"
            print(f"skipped run_id={run_id} reason={note}", flush=True)
            return 0

        mode = "snapshot-only" if args.only_snapshot else "history+snapshot"
        note = f"daily watchlist {mode} {run_summary(jobs_to_run)}"
        print(
            f"run_id={run_id} db={cfg.db_path} "
            f"jobs={run_summary(jobs_to_run)}",
            flush=True,
        )
        quote_ctx = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
        api = ApiRunner(quote_ctx, cfg.sleep_seconds)
        try:
            run_backfill(
                api,
                store,
                jobs_to_run,
                args.history_start,
                args.snapshot_batch_size,
                args.only_snapshot,
            )
        finally:
            quote_ctx.close()
    except Exception as exc:
        status = "failed"
        note = f"{type(exc).__name__}: {exc}"
        store.log_error(None, "daily_run", "main", note)
        raise
    finally:
        if started:
            store.finish_run(status, note)
        store.close()
    print(f"finished run_id={run_id} status={status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
