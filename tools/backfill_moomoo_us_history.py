from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import moomoo as ft
import pandas as pd

from moomoo_rate_limits import (
    DEFAULT_BACKFILL_SLEEP_SECONDS,
    DEFAULT_OPTION_CHAIN_SLEEP_SECONDS,
)


DEFAULT_START = "2024-01-01"
DEFAULT_DB = "us_strategy/history_data.db"
DEFAULT_WATCHLIST = "us_strategy/watchlist.txt"

META_COLUMNS = {
    "_code",
    "_row_key",
    "_run_id",
    "_fetched_at",
    "_payload_json",
}


@dataclass(frozen=True)
class BackfillConfig:
    """Runtime options for a read-only moomoo history backfill."""

    codes: tuple[str, ...]
    start: str
    end: str
    db_path: Path
    host: str
    port: int
    sleep_seconds: float
    page_size: int
    max_pages: int
    option_contracts_per_expiry: int
    max_option_contracts_per_code: int
    option_chain_sleep_seconds: float
    option_history_sleep_seconds: float
    refresh_existing_options: bool
    include_fundamentals: bool
    only_options: bool


def utc_now() -> str:
    """Return an ISO timestamp in UTC."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_watchlist(path: Path) -> tuple[str, ...]:
    """Load US symbols from a watchlist file without importing strategy code."""

    raw_codes: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.split("#", 1)[0].strip()
            if not line:
                continue
            raw_codes.extend(part.strip() for part in line.split(","))

    seen: set[str] = set()
    codes: list[str] = []
    for code in raw_codes:
        if code and code.startswith("US.") and code not in seen:
            seen.add(code)
            codes.append(code)
    return tuple(codes)


def parse_codes(raw: str, watchlist: Path) -> tuple[str, ...]:
    """Parse explicit codes or fall back to the repository watchlist."""

    if raw.strip():
        codes = tuple(
            code.strip()
            for code in raw.split(",")
            if code.strip() and code.strip().startswith("US.")
        )
    else:
        codes = load_watchlist(watchlist)
    if not codes:
        raise ValueError("no US symbols found")
    return codes


def normalize_value(value: Any) -> Any:
    """Convert pandas/numpy values into SQLite and JSON friendly scalars."""

    if value is None:
        return None
    if isinstance(value, (list, tuple, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize a row dict for stable storage."""

    return {str(key): normalize_value(value) for key, value in record.items()}


def safe_column(name: str) -> str:
    """Map an arbitrary DataFrame column name to a stable SQLite column."""

    cleaned = re.sub(r"[^0-9A-Za-z_]+", "_", str(name)).strip("_").lower()
    if not cleaned:
        cleaned = "field"
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    if cleaned in META_COLUMNS:
        cleaned = f"data{cleaned}"
    return cleaned[:80]


def quote_ident(name: str) -> str:
    """Quote a SQLite identifier."""

    return '"' + name.replace('"', '""') + '"'


class HistoryStore:
    """SQLite sink that stores expanded columns and raw JSON payloads."""

    def __init__(self, db_path: Path, run_id: str) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.run_id = run_id
        self.conn = sqlite3.connect(str(db_path))
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self._init_meta()

    def close(self) -> None:
        """Commit and close the SQLite connection."""

        self.conn.commit()
        self.conn.close()

    def _init_meta(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS backfill_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                start_date TEXT NOT NULL,
                end_date TEXT NOT NULL,
                codes_json TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS api_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                code TEXT,
                dataset TEXT NOT NULL,
                api TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_stats (
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                code TEXT,
                table_name TEXT NOT NULL,
                rows_seen INTEGER NOT NULL,
                note TEXT,
                PRIMARY KEY (run_id, code, table_name, note)
            )
            """
        )

    def start_run(self, cfg: BackfillConfig) -> None:
        """Insert the run header."""

        self.conn.execute(
            """
            INSERT OR REPLACE INTO backfill_runs
                (run_id, started_at, start_date, end_date, codes_json, status, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                self.run_id,
                utc_now(),
                cfg.start,
                cfg.end,
                json.dumps(cfg.codes, ensure_ascii=False),
                "running",
                "read-only moomoo API history backfill",
            ),
        )
        self.conn.commit()

    def finish_run(self, status: str, note: str | None = None) -> None:
        """Mark the run as finished."""

        self.conn.execute(
            """
            UPDATE backfill_runs
               SET finished_at = ?, status = ?, note = COALESCE(?, note)
             WHERE run_id = ?
            """,
            (utc_now(), status, note, self.run_id),
        )
        self.conn.commit()

    def log_error(
        self,
        code: str | None,
        dataset: str,
        api: str,
        message: str,
    ) -> None:
        """Record an API error without hiding it from the final summary."""

        self.conn.execute(
            """
            INSERT INTO api_errors (run_id, ts, code, dataset, api, message)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.run_id, utc_now(), code, dataset, api, message[:2000]),
        )
        self.conn.commit()

    def add_stat(
        self,
        code: str | None,
        table_name: str,
        rows_seen: int,
        note: str | None = None,
    ) -> None:
        """Record how many rows were seen for a dataset in this run."""

        self.conn.execute(
            """
            INSERT OR REPLACE INTO dataset_stats
                (run_id, ts, code, table_name, rows_seen, note)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.run_id, utc_now(), code, table_name, rows_seen, note),
        )

    def _ensure_table(self, table_name: str, columns: Iterable[str]) -> None:
        table = quote_ident(table_name)
        self.conn.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table} (
                _code TEXT NOT NULL,
                _row_key TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL,
                _payload_json TEXT NOT NULL,
                PRIMARY KEY (_code, _row_key)
            )
            """
        )
        existing = {
            row[1]
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column in columns:
            if column not in existing:
                self.conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {quote_ident(column)}"
                )
                existing.add(column)

    def store_records(
        self,
        table_name: str,
        code: str,
        records: Iterable[dict[str, Any]],
        key_fields: Iterable[str],
        extra: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> int:
        """Upsert raw records into a dataset table."""

        rows = []
        extra = normalize_record(extra or {})
        key_fields = tuple(key_fields)
        for record in records:
            row = {**extra, **normalize_record(record)}
            row_key = self._row_key(row, key_fields)
            payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
            rows.append((row_key, payload, row))

        if not rows:
            self.add_stat(code, table_name, 0, note)
            return 0

        raw_columns = sorted({key for _, _, row in rows for key in row})
        column_map = self._column_map(raw_columns)
        self._ensure_table(table_name, column_map.values())

        insert_columns = [
            "_code",
            "_row_key",
            "_run_id",
            "_fetched_at",
            "_payload_json",
            *column_map.values(),
        ]
        placeholders = ", ".join("?" for _ in insert_columns)
        set_clause = ", ".join(
            f"{quote_ident(col)} = excluded.{quote_ident(col)}"
            for col in insert_columns
            if col not in {"_code", "_row_key"}
        )
        sql = (
            f"INSERT INTO {quote_ident(table_name)} "
            f"({', '.join(quote_ident(c) for c in insert_columns)}) "
            f"VALUES ({placeholders}) "
            f"ON CONFLICT(_code, _row_key) DO UPDATE SET {set_clause}"
        )
        fetched_at = utc_now()
        values = []
        for row_key, payload, row in rows:
            values.append(
                [
                    code,
                    row_key,
                    self.run_id,
                    fetched_at,
                    payload,
                    *[row.get(raw_col) for raw_col in column_map],
                ]
            )
        self.conn.executemany(sql, values)
        self.add_stat(code, table_name, len(values), note)
        self.conn.commit()
        return len(values)

    def store_frame(
        self,
        table_name: str,
        code: str,
        frame: pd.DataFrame,
        key_fields: Iterable[str],
        extra: dict[str, Any] | None = None,
        note: str | None = None,
    ) -> int:
        """Store a DataFrame as records."""

        if frame is None or frame.empty:
            self.add_stat(code, table_name, 0, note)
            return 0
        records = frame.where(pd.notnull(frame), None).to_dict("records")
        return self.store_records(table_name, code, records, key_fields, extra, note)

    @staticmethod
    def _column_map(raw_columns: Iterable[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        used: set[str] = set()
        for raw in raw_columns:
            base = safe_column(raw)
            column = base
            i = 2
            while column in used:
                column = f"{base[:70]}_{i}"
                i += 1
            mapping[raw] = column
            used.add(column)
        return mapping

    @staticmethod
    def _row_key(row: dict[str, Any], key_fields: Iterable[str]) -> str:
        parts = [str(row.get(field, "")) for field in key_fields]
        if any(parts):
            return "|".join(parts)
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ApiRunner:
    """Small wrapper that rate-limits read-only moomoo API calls."""

    def __init__(self, ctx: ft.OpenQuoteContext, sleep_seconds: float) -> None:
        self.ctx = ctx
        self.sleep_seconds = sleep_seconds

    def call(self, func: Callable[[], Any]) -> Any:
        """Run one API call and sleep afterwards."""

        try:
            return func()
        finally:
            if self.sleep_seconds > 0:
                time.sleep(self.sleep_seconds)


def ok_or_error(ret: Any, data: Any) -> tuple[bool, str]:
    """Return a simple RET_OK check result."""

    if ret == ft.RET_OK:
        return True, ""
    return False, str(data)


def filter_since(
    frame: pd.DataFrame,
    start: str,
    date_columns: tuple[str, ...],
) -> pd.DataFrame:
    """Keep rows whose first available date column is on or after start."""

    if frame.empty:
        return frame
    for column in date_columns:
        if column in frame.columns:
            values = frame[column].astype(str).str.slice(0, 10)
            return frame[values >= start].copy()
    return frame


def fetch_trading_days(
    api: ApiRunner,
    store: HistoryStore,
    cfg: BackfillConfig,
) -> None:
    """Backfill the US trading calendar for the requested window."""

    ret, data = api.call(
        lambda: api.ctx.request_trading_days(
            market=ft.TradeDateMarket.US,
            start=cfg.start,
            end=cfg.end,
        )
    )
    ok, msg = ok_or_error(ret, data)
    if not ok:
        store.log_error(None, "trading_days", "request_trading_days", msg)
        return
    records = data if isinstance(data, list) else []
    rows = [dict(item) for item in records]
    count = store.store_records(
        "trading_days",
        "US",
        rows,
        key_fields=("time", "trade_date_type"),
    )
    print(f"[calendar] rows={count}", flush=True)


def fetch_quota(api: ApiRunner, store: HistoryStore, label: str) -> None:
    """Store a historical K-line quota snapshot."""

    ret, data = api.call(lambda: api.ctx.get_history_kl_quota(get_detail=True))
    ok, msg = ok_or_error(ret, data)
    if not ok:
        store.log_error(None, "history_kl_quota", "get_history_kl_quota", msg)
        return
    if not isinstance(data, tuple) or len(data) < 3:
        store.store_records(
            "history_kl_quota",
            "ACCOUNT",
            [{"label": label, "raw": str(data)}],
            key_fields=("label",),
        )
        return
    used, remain, details = data[0], data[1], data[2]
    rows = [
        {
            "label": label,
            "used_quota": used,
            "remain_quota": remain,
            "detail_count": len(details),
            "detail_json": details,
        }
    ]
    store.store_records(
        "history_kl_quota",
        "ACCOUNT",
        rows,
        key_fields=("label",),
    )
    store.store_records(
        "history_kl_quota_detail",
        "ACCOUNT",
        [dict(item, label=label) for item in details],
        key_fields=("label", "code"),
    )
    print(f"[quota:{label}] used={used} remain={remain}", flush=True)


def fetch_basic_info(api: ApiRunner, store: HistoryStore, code: str) -> None:
    """Fetch basic stock or ETF metadata."""

    frames: list[pd.DataFrame] = []
    for security_type in (ft.SecurityType.STOCK, ft.SecurityType.ETF):
        ret, data = api.call(
            lambda st=security_type: api.ctx.get_stock_basicinfo(
                ft.Market.US,
                st,
                [code],
            )
        )
        ok, msg = ok_or_error(ret, data)
        if ok and isinstance(data, pd.DataFrame) and not data.empty:
            frames.append(data.assign(request_security_type=security_type))
        elif not ok:
            store.log_error(code, "stock_basicinfo", "get_stock_basicinfo", msg)
    if frames:
        frame = pd.concat(frames, ignore_index=True)
        store.store_frame(
            "stock_basicinfo",
            code,
            frame,
            key_fields=("code", "stock_type", "request_security_type"),
        )


def fetch_history_kline(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Fetch adjusted daily candlesticks for backtesting."""

    page_key: bytes | None = None
    total = 0
    pages = 0
    while True:
        ret, data, next_key = api.call(
            lambda key=page_key: api.ctx.request_history_kline(
                code,
                start=cfg.start,
                end=cfg.end,
                ktype=ft.KLType.K_DAY,
                autype=ft.AuType.QFQ,
                max_count=cfg.page_size,
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
                "ktype": ft.KLType.K_DAY,
                "autype": ft.AuType.QFQ,
                "session": ft.Session.NONE,
                "extended_time": False,
            },
        )
        total += count
        pages += 1
        if not next_key or pages >= cfg.max_pages:
            break
        page_key = next_key
    print(f"[{code}] history_kline_day rows={total} pages={pages}", flush=True)


def fetch_capital_flow(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Fetch daily capital flow rows available from moomoo."""

    ret, data = api.call(
        lambda: api.ctx.get_capital_flow(
            code,
            period_type=ft.PeriodType.DAY,
            start=cfg.start,
            end=cfg.end,
        )
    )
    ok, msg = ok_or_error(ret, data)
    if not ok:
        store.log_error(code, "capital_flow_day", "get_capital_flow", msg)
        return
    frame = filter_since(data, cfg.start, ("capital_flow_item_time",))
    count = store.store_frame(
        "capital_flow_day",
        code,
        frame,
        key_fields=("period_type", "capital_flow_item_time"),
        extra={"period_type": ft.PeriodType.DAY},
    )
    print(f"[{code}] capital_flow_day rows={count}", flush=True)


def next_key_from(frame: pd.DataFrame) -> str:
    """Read a moomoo pagination key stored in DataFrame attrs."""

    value = frame.attrs.get("next_key") if isinstance(frame, pd.DataFrame) else None
    return "-1" if value in (None, "") else str(value)


def fetch_daily_short_volume(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Fetch US daily short volume with pagination."""

    next_key: str | None = None
    total = 0
    pages = 0
    while pages < cfg.max_pages:
        ret, us_df, _hk_df = api.call(
            lambda key=next_key: api.ctx.get_daily_short_volume(
                code,
                next_key=key,
                num=50,
            )
        )
        ok, msg = ok_or_error(ret, us_df)
        if not ok:
            store.log_error(
                code,
                "daily_short_volume",
                "get_daily_short_volume",
                msg,
            )
            break
        frame = filter_since(us_df, cfg.start, ("timestamp_str",))
        total += store.store_frame(
            "daily_short_volume",
            code,
            frame,
            key_fields=("timestamp_str",),
        )
        pages += 1
        oldest = (
            str(us_df["timestamp_str"].min())[:10]
            if isinstance(us_df, pd.DataFrame)
            and not us_df.empty
            and "timestamp_str" in us_df.columns
            else ""
        )
        next_key = next_key_from(us_df)
        if next_key == "-1" or (oldest and oldest < cfg.start):
            break
    print(f"[{code}] daily_short_volume rows={total} pages={pages}", flush=True)


def fetch_short_interest(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Fetch US short interest with pagination."""

    next_key: str | None = None
    total = 0
    pages = 0
    while pages < cfg.max_pages:
        ret, us_df, _hk_df = api.call(
            lambda key=next_key: api.ctx.get_short_interest(
                code,
                next_key=key,
                num=50,
            )
        )
        ok, msg = ok_or_error(ret, us_df)
        if not ok:
            store.log_error(code, "short_interest", "get_short_interest", msg)
            break
        frame = filter_since(us_df, cfg.start, ("timestamp_str",))
        total += store.store_frame(
            "short_interest",
            code,
            frame,
            key_fields=("timestamp_str",),
        )
        pages += 1
        oldest = (
            str(us_df["timestamp_str"].min())[:10]
            if isinstance(us_df, pd.DataFrame)
            and not us_df.empty
            and "timestamp_str" in us_df.columns
            else ""
        )
        next_key = next_key_from(us_df)
        if next_key == "-1" or (oldest and oldest < cfg.start):
            break
    print(f"[{code}] short_interest rows={total} pages={pages}", flush=True)


def fetch_rehab(api: ApiRunner, store: HistoryStore, code: str) -> None:
    """Fetch split/dividend adjustment factors."""

    ret, data = api.call(lambda: api.ctx.get_rehab(code))
    ok, msg = ok_or_error(ret, data)
    if not ok:
        store.log_error(code, "rehab", "get_rehab", msg)
        return
    count = store.store_frame(
        "rehab",
        code,
        data,
        key_fields=("ex_div_date", "company_act_flag"),
    )
    print(f"[{code}] rehab rows={count}", flush=True)


def fetch_holding_changes(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Fetch the older US holder change endpoint for all holder categories."""

    holder_types = (ft.StockHolder.INSTITUTE, ft.StockHolder.FUND, ft.StockHolder.EXECUTIVE)
    total = 0
    for holder_type in holder_types:
        ret, data = api.call(
            lambda ht=holder_type: api.ctx.get_holding_change_list(
                code,
                holder_type=ht,
                start=cfg.start,
                end=cfg.end,
            )
        )
        ok, msg = ok_or_error(ret, data)
        if not ok:
            store.log_error(
                code,
                "holding_change_list",
                "get_holding_change_list",
                f"{holder_type}: {msg}",
            )
            continue
        total += store.store_frame(
            "holding_change_list",
            code,
            data,
            key_fields=("holder_type", "holder_name", "time"),
            extra={"holder_type": holder_type},
        )
    print(f"[{code}] holding_change_list rows={total}", flush=True)


def dict_list_frame(payload: dict[str, Any], key: str) -> pd.DataFrame:
    """Build a DataFrame from a list stored in a dict key."""

    values = payload.get(key, []) if isinstance(payload, dict) else []
    if isinstance(values, pd.DataFrame):
        return values
    if not values:
        return pd.DataFrame()
    return pd.DataFrame(values)


def fetch_option_data(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    latest_close: float | None,
    cfg: BackfillConfig,
) -> None:
    """Fetch option expirations, current chains, and near-ATM history."""

    ret, exp = api.call(lambda: api.ctx.get_option_expiration_date(code=code))
    ok, msg = ok_or_error(ret, exp)
    if not ok or not isinstance(exp, pd.DataFrame) or exp.empty:
        if not ok:
            store.log_error(code, "option_expiration", "get_option_expiration_date", msg)
        print(f"[{code}] option_expiration rows=0", flush=True)
        return

    exp = exp.copy()
    exp["underlying"] = code
    exp_count = store.store_frame(
        "option_expiration",
        code,
        exp,
        key_fields=("strike_time", "expiration_cycle"),
    )

    selected_contracts: list[str] = []
    chain_rows = 0
    for _, row in exp.iterrows():
        expiry = str(row.get("strike_time", ""))[:10]
        if not expiry:
            continue
        if (
            cfg.only_options
            and not cfg.refresh_existing_options
            and option_chain_exists(store, code, expiry)
        ):
            continue
        if cfg.option_chain_sleep_seconds > 0:
            time.sleep(cfg.option_chain_sleep_seconds)
        ret2, chain = api.call(
            lambda ex=expiry: api.ctx.get_option_chain(code, start=ex, end=ex)
        )
        ok2, msg2 = ok_or_error(ret2, chain)
        if not ok2:
            store.log_error(code, "option_chain", "get_option_chain", msg2)
            continue
        if not isinstance(chain, pd.DataFrame) or chain.empty:
            continue
        chain = chain.copy()
        chain["underlying"] = code
        chain["chain_expiry"] = expiry
        chain_rows += store.store_frame(
            "option_chain",
            code,
            chain,
            key_fields=("chain_expiry", "code"),
        )
        selected_contracts.extend(
            select_option_contracts(
                chain,
                latest_close,
                cfg.option_contracts_per_expiry,
            )
        )
        selected_contracts = unique(selected_contracts)[
            : cfg.max_option_contracts_per_code
        ]

    iv_rows = 0
    prob_rows = 0
    for option_code in selected_contracts:
        if cfg.option_history_sleep_seconds > 0:
            time.sleep(cfg.option_history_sleep_seconds)
        ret3, iv = api.call(
            lambda oc=option_code: api.ctx.get_option_volatility(
                oc,
                query_time_period=None,
                hv_time_period=30,
            )
        )
        ok3, msg3 = ok_or_error(ret3, iv)
        if ok3 and isinstance(iv, pd.DataFrame):
            iv = filter_since(iv, cfg.start, ("timestamp_str",))
            iv_rows += store.store_frame(
                "option_volatility",
                code,
                iv,
                key_fields=("option_code", "timestamp_str"),
                extra={"option_code": option_code},
            )
        elif not ok3:
            store.log_error(
                code,
                "option_volatility",
                "get_option_volatility",
                f"{option_code}: {msg3}",
            )

        if cfg.option_history_sleep_seconds > 0:
            time.sleep(cfg.option_history_sleep_seconds)
        ret4, prob = api.call(
            lambda oc=option_code: api.ctx.get_option_exercise_probability(oc)
        )
        ok4, msg4 = ok_or_error(ret4, prob)
        if ok4 and isinstance(prob, pd.DataFrame):
            prob = filter_since(prob, cfg.start, ("timestamp_str",))
            prob_rows += store.store_frame(
                "option_exercise_probability",
                code,
                prob,
                key_fields=("option_code", "timestamp_str"),
                extra={"option_code": option_code},
            )
        elif not ok4:
            store.log_error(
                code,
                "option_exercise_probability",
                "get_option_exercise_probability",
                f"{option_code}: {msg4}",
            )
    print(
        f"[{code}] option_expiration={exp_count} option_chain={chain_rows} "
        f"option_iv={iv_rows} option_prob={prob_rows} "
        f"contracts={len(selected_contracts)}",
        flush=True,
    )


def select_option_contracts(
    chain: pd.DataFrame,
    latest_close: float | None,
    contracts_per_expiry: int,
) -> list[str]:
    """Select a small near-ATM subset for per-contract history APIs."""

    if contracts_per_expiry <= 0 or "code" not in chain.columns:
        return []
    frame = chain.copy()
    if latest_close and "strike_price" in frame.columns:
        distance = (
            pd.to_numeric(frame["strike_price"], errors="coerce") - latest_close
        ).abs()
        frame = frame.assign(_distance=distance)
        frame = frame.sort_values(["_distance", "code"])
    else:
        frame = frame.sort_values("code")
    return [str(value) for value in frame["code"].head(contracts_per_expiry)]


def option_chain_exists(store: HistoryStore, code: str, expiry: str) -> bool:
    """Return whether a chain snapshot for one expiry is already stored."""

    if not table_exists(store.conn, "option_chain"):
        return False
    row = store.conn.execute(
        """
        SELECT 1
          FROM option_chain
         WHERE _code = ?
           AND chain_expiry = ?
         LIMIT 1
        """,
        (code, expiry),
    ).fetchone()
    return row is not None


def unique(values: Iterable[str]) -> list[str]:
    """Return unique values while preserving order."""

    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def fetch_financial_dict_page(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    table_name: str,
    api_name: str,
    func: Callable[[str | None], tuple[Any, Any]],
    list_keys: tuple[str, ...],
    key_fields: tuple[str, ...],
    max_pages: int,
) -> int:
    """Fetch a paginated dict API and store each list key as records."""

    next_key: str | None = None
    total = 0
    for _ in range(max_pages):
        ret, payload = api.call(lambda key=next_key: func(key))
        ok, msg = ok_or_error(ret, payload)
        if not ok:
            store.log_error(code, table_name, api_name, msg)
            break
        if not isinstance(payload, dict):
            store.store_records(
                f"{table_name}_raw",
                code,
                [{"raw": str(payload)}],
                key_fields=("raw",),
            )
            break
        for list_key in list_keys:
            frame = dict_list_frame(payload, list_key)
            total += store.store_frame(
                f"{table_name}_{list_key}",
                code,
                frame,
                key_fields=key_fields,
                extra={"list_key": list_key},
            )
        next_key = str(payload.get("next_key", "-1") or "-1")
        if next_key == "-1":
            break
    return total


def fetch_fundamentals(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Best-effort fetch of historical/event APIs useful for backtests."""

    simple_calls: tuple[
        tuple[str, str, Callable[[], tuple[Any, Any]], tuple[str, ...]],
        ...,
    ] = (
        (
            "financials_earnings_price_history",
            "get_financials_earnings_price_history",
            lambda: api.ctx.get_financials_earnings_price_history(code),
            ("fiscal_year", "financial_type", "schedule_date"),
        ),
        (
            "financials_earnings_price_move",
            "get_financials_earnings_price_move",
            lambda: api.ctx.get_financials_earnings_price_move(code, period_count=20),
            ("fiscal_year", "financial_type", "schedule_date"),
        ),
        (
            "company_profile",
            "get_company_profile",
            lambda: api.ctx.get_company_profile(code),
            ("name", "field_type"),
        ),
        (
            "company_executives",
            "get_company_executives",
            lambda: api.ctx.get_company_executives(code),
            ("leader_name", "position_name", "begin_date_str"),
        ),
        (
            "company_operational_efficiency",
            "get_company_operational_efficiency",
            lambda: api.ctx.get_company_operational_efficiency(code, num=50),
            ("period_text",),
        ),
        (
            "shareholders_institutional",
            "get_shareholders_institutional",
            lambda: api.ctx.get_shareholders_institutional(code, num=50),
            ("period_text",),
        ),
        (
            "shareholders_holding_changes",
            "get_shareholders_holding_changes",
            lambda: api.ctx.get_shareholders_holding_changes(code, num=50),
            ("period_text", "holder_id", "holding_date_str"),
        ),
        (
            "insider_holder_list",
            "get_insider_holder_list",
            lambda: api.ctx.get_insider_holder_list(code, num=20),
            ("holder_id", "name"),
        ),
        (
            "insider_trade_list",
            "get_insider_trade_list",
            lambda: api.ctx.get_insider_trade_list(code, num=50),
            ("holder_id", "transaction_type", "min_trade_date_str", "max_trade_date_str"),
        ),
    )
    total = 0
    for table_name, api_name, func, key_fields in simple_calls:
        ret, data = api.call(func)
        ok, msg = ok_or_error(ret, data)
        if not ok:
            store.log_error(code, table_name, api_name, msg)
            continue
        if isinstance(data, pd.DataFrame):
            total += store.store_frame(table_name, code, data, key_fields)
        elif isinstance(data, dict):
            total += store.store_records(
                f"{table_name}_raw",
                code,
                [data],
                key_fields=("next_key", "update_time", "update_time_str"),
            )
        else:
            total += store.store_records(
                f"{table_name}_raw",
                code,
                [{"raw": str(data)}],
                key_fields=("raw",),
            )

    fetch_financial_statements(api, store, code, cfg.max_pages)
    fetch_revenue_breakdown(api, store, code)
    fetch_corporate_actions(api, store, code, cfg)
    fetch_shareholders_overview(api, store, code)
    print(f"[{code}] fundamentals base_rows={total}", flush=True)


def fetch_financial_statements(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    max_pages: int,
) -> None:
    """Fetch income, balance sheet, cash flow, and main index statements."""

    statement_types = {
        "income": 1,
        "balance_sheet": 2,
        "cash_flow": 3,
        "main_index": 4,
    }
    for name, statement_type in statement_types.items():
        next_key: str | None = None
        for _ in range(max_pages):
            ret, payload = api.call(
                lambda key=next_key, st=statement_type: api.ctx.get_financials_statements(
                    code,
                    statement_type=st,
                    next_key=key,
                    num=50,
                )
            )
            ok, msg = ok_or_error(ret, payload)
            if not ok:
                store.log_error(
                    code,
                    "financials_statements",
                    "get_financials_statements",
                    f"{name}: {msg}",
                )
                break
            if not isinstance(payload, dict):
                break
            structures = payload.get("structure_list", [])
            store.store_records(
                "financials_statement_structure",
                code,
                structures,
                key_fields=("statement_type", "field_id"),
                extra={"statement_type": name},
            )
            reports = payload.get("report_list", [])
            report_rows: list[dict[str, Any]] = []
            item_rows: list[dict[str, Any]] = []
            for report in reports:
                report_dict = dict(report)
                items = report_dict.pop("item_list", []) or []
                report_key = "|".join(
                    str(report_dict.get(k, ""))
                    for k in ("date_time_str", "fiscal_year", "financial_type")
                )
                report_dict["statement_type"] = name
                report_dict["report_key"] = report_key
                report_rows.append(report_dict)
                for item in items:
                    row = dict(item)
                    row["statement_type"] = name
                    row["report_key"] = report_key
                    row["period_text"] = report_dict.get("period_text")
                    row["date_time_str"] = report_dict.get("date_time_str")
                    item_rows.append(row)
            store.store_records(
                "financials_statement_report",
                code,
                report_rows,
                key_fields=("statement_type", "report_key"),
            )
            store.store_records(
                "financials_statement_item",
                code,
                item_rows,
                key_fields=("statement_type", "report_key", "field_id"),
            )
            next_key = str(payload.get("next_key", "-1") or "-1")
            if next_key == "-1":
                break


def fetch_revenue_breakdown(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
) -> None:
    """Fetch latest available revenue breakdown payload."""

    ret, payload = api.call(lambda: api.ctx.get_financials_revenue_breakdown(code))
    ok, msg = ok_or_error(ret, payload)
    if not ok:
        store.log_error(
            code,
            "financials_revenue_breakdown",
            "get_financials_revenue_breakdown",
            msg,
        )
        return
    if isinstance(payload, dict):
        for key in ("product", "industry", "region", "business"):
            frame = dict_list_frame(payload, f"{key}_list")
            store.store_frame(
                f"financials_revenue_breakdown_{key}",
                code,
                frame,
                key_fields=("date", "name", "revenue_breakdown_type"),
                extra={"breakdown_type": key},
            )
        store.store_records(
            "financials_revenue_breakdown_raw",
            code,
            [payload],
            key_fields=("date", "financial_type"),
        )
    elif isinstance(payload, pd.DataFrame):
        store.store_frame(
            "financials_revenue_breakdown",
            code,
            payload,
            key_fields=("date", "name"),
        )


def fetch_corporate_actions(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Fetch split, dividend, and buyback events."""

    fetch_financial_dict_page(
        api,
        store,
        code,
        "corporate_actions_stock_splits",
        "get_corporate_actions_stock_splits",
        lambda key: api.ctx.get_corporate_actions_stock_splits(
            code,
            next_key=key,
            num=50,
        ),
        ("split_list",),
        ("ex_date", "split_base", "split_ert"),
        cfg.max_pages,
    )
    fetch_financial_dict_page(
        api,
        store,
        code,
        "corporate_actions_buybacks",
        "get_corporate_actions_buybacks",
        lambda key: api.ctx.get_corporate_actions_buybacks(
            code,
            next_key=key,
            num=50,
        ),
        ("hk_buy_back_list", "a_buy_back_list"),
        ("publ_date_str", "change_date_str", "share_type"),
        cfg.max_pages,
    )
    ret, payload = api.call(lambda: api.ctx.get_corporate_actions_dividends(code))
    ok, msg = ok_or_error(ret, payload)
    if not ok:
        store.log_error(
            code,
            "corporate_actions_dividends",
            "get_corporate_actions_dividends",
            msg,
        )
        return
    if isinstance(payload, dict):
        frame = dict_list_frame(payload, "dividend_list")
        store.store_frame(
            "corporate_actions_dividends",
            code,
            frame,
            key_fields=("ex_date", "record_date", "statement"),
        )
        store.store_records(
            "corporate_actions_dividends_raw",
            code,
            [payload],
            key_fields=("next_key",),
        )


def fetch_shareholders_overview(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
) -> None:
    """Fetch holder overview and available holding periods."""

    ret, payload = api.call(lambda: api.ctx.get_shareholders_overview(code, period_id=0))
    ok, msg = ok_or_error(ret, payload)
    if not ok:
        store.log_error(
            code,
            "shareholders_overview",
            "get_shareholders_overview",
            msg,
        )
        return
    if isinstance(payload, dict):
        for key in ("main_holder", "holder_type", "holding_period"):
            frame = payload.get(key)
            if isinstance(frame, pd.DataFrame):
                store.store_frame(
                    f"shareholders_overview_{key}",
                    code,
                    frame,
                    key_fields=("static_date_str", "name", "period_id", "holder_id"),
                )


def latest_close_from_db(store: HistoryStore, code: str) -> float | None:
    """Read the latest close from the stored daily K-line table."""

    if not table_exists(store.conn, "history_kline"):
        return None
    row = store.conn.execute(
        """
        SELECT close
          FROM history_kline
         WHERE _code = ?
           AND ktype = ?
           AND close IS NOT NULL
         ORDER BY time_key DESC
         LIMIT 1
        """,
        (code, ft.KLType.K_DAY),
    ).fetchone()
    if row is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


def table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    """Return whether a table exists in SQLite."""

    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    ).fetchone()
    return row is not None


def backfill_code(
    api: ApiRunner,
    store: HistoryStore,
    code: str,
    cfg: BackfillConfig,
) -> None:
    """Backfill all configured datasets for one US symbol."""

    print(f"\n=== {code} ===", flush=True)
    if cfg.only_options:
        latest_close = latest_close_from_db(store, code)
        fetch_option_data(api, store, code, latest_close, cfg)
        return
    fetch_basic_info(api, store, code)
    fetch_history_kline(api, store, code, cfg)
    fetch_capital_flow(api, store, code, cfg)
    fetch_daily_short_volume(api, store, code, cfg)
    fetch_short_interest(api, store, code, cfg)
    fetch_rehab(api, store, code)
    fetch_holding_changes(api, store, code, cfg)
    latest_close = latest_close_from_db(store, code)
    fetch_option_data(api, store, code, latest_close, cfg)
    if cfg.include_fundamentals:
        fetch_fundamentals(api, store, code, cfg)


def parse_args() -> argparse.Namespace:
    """Parse command line options."""

    parser = argparse.ArgumentParser(
        description="Backfill moomoo US historical datasets into SQLite.",
    )
    parser.add_argument("--codes", default="", help="Comma-separated US symbols.")
    parser.add_argument("--watchlist", default=DEFAULT_WATCHLIST)
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=datetime.now().date().isoformat())
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--sleep", type=float, default=DEFAULT_BACKFILL_SLEEP_SECONDS)
    parser.add_argument("--page-size", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=80)
    parser.add_argument("--option-contracts-per-expiry", type=int, default=2)
    parser.add_argument("--max-option-contracts-per-code", type=int, default=40)
    parser.add_argument(
        "--option-chain-sleep",
        type=float,
        default=DEFAULT_OPTION_CHAIN_SLEEP_SECONDS,
    )
    parser.add_argument("--option-history-sleep", type=float, default=0.0)
    parser.add_argument(
        "--refresh-existing-options",
        action="store_true",
        help="Refetch option chains even when an expiry already exists.",
    )
    parser.add_argument(
        "--only-options",
        action="store_true",
        help="Only fetch option datasets; useful for rate-limit repairs.",
    )
    parser.add_argument(
        "--skip-fundamentals",
        action="store_true",
        help="Skip financial/company/shareholder best-effort APIs.",
    )
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> BackfillConfig:
    """Build a validated immutable config object."""

    codes = parse_codes(args.codes, Path(args.watchlist))
    return BackfillConfig(
        codes=codes,
        start=args.start,
        end=args.end,
        db_path=Path(args.db),
        host=args.host,
        port=args.port,
        sleep_seconds=args.sleep,
        page_size=args.page_size,
        max_pages=args.max_pages,
        option_contracts_per_expiry=args.option_contracts_per_expiry,
        max_option_contracts_per_code=args.max_option_contracts_per_code,
        option_chain_sleep_seconds=args.option_chain_sleep,
        option_history_sleep_seconds=args.option_history_sleep,
        refresh_existing_options=args.refresh_existing_options,
        include_fundamentals=not args.skip_fundamentals,
        only_options=args.only_options,
    )


def main() -> int:
    """Run the backfill from moomoo OpenD into a local SQLite database."""

    args = parse_args()
    cfg = build_config(args)
    run_id = uuid.uuid4().hex
    store = HistoryStore(cfg.db_path, run_id)
    store.start_run(cfg)
    print(
        f"run_id={run_id} db={cfg.db_path} start={cfg.start} "
        f"end={cfg.end} codes={len(cfg.codes)}",
        flush=True,
    )
    quote_ctx = ft.OpenQuoteContext(host=cfg.host, port=cfg.port)
    api = ApiRunner(quote_ctx, cfg.sleep_seconds)
    status = "success"
    note = None
    try:
        fetch_quota(api, store, "before")
        fetch_trading_days(api, store, cfg)
        for code in cfg.codes:
            backfill_code(api, store, code, cfg)
        fetch_quota(api, store, "after")
    except Exception as exc:
        status = "failed"
        note = f"{type(exc).__name__}: {exc}"
        store.log_error(None, "run", "main", note)
        raise
    finally:
        quote_ctx.close()
        store.finish_run(status, note)
        store.close()
    print(f"finished run_id={run_id} status={status}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
