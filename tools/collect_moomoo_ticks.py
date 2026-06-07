from __future__ import annotations

import argparse
import json
import math
import queue
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

import moomoo as ft
import pandas as pd

from dark_pool_proxy import (
    DarkPoolProxyConfig,
    DarkPoolProxyMetrics,
    DarkPoolProxyTracker,
)
from order_book_l2 import (
    L2ImbalanceConfig,
    L2ImbalanceSignal,
    L2ImbalanceTracker,
    build_order_book_records,
    compute_order_book_metrics,
)
from tools.backfill_moomoo_us_history import DEFAULT_DB


DEFAULT_US_WATCHLIST = "us_strategy/watchlist.txt"
DEFAULT_HK_WATCHLIST = "hk_strategy/watchlist.txt"
US_TZ = ZoneInfo("America/New_York")
HK_TZ = ZoneInfo("Asia/Hong_Kong")


@dataclass(frozen=True)
class MarketConfig:
    """Market-specific tick subscription settings."""

    label: str
    prefix: str
    watchlist_path: Path
    timezone: ZoneInfo
    session: str


@dataclass(frozen=True)
class TickBatch:
    """A batch of ticker rows from cache or push."""

    frame: pd.DataFrame
    source: str


@dataclass(frozen=True)
class OrderBookBatch:
    """A full L2 order book snapshot from cache or push."""

    data: dict[str, Any]
    source: str


@dataclass
class PeriodicPoll:
    """A low-frequency polling callback used during realtime collection."""

    callback: Callable[[], None]
    interval: float
    next_due: float


def utc_now() -> str:
    """Return the current UTC timestamp for run audit fields."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def normalize_value(value: Any) -> Any:
    """Convert pandas/numpy scalars into JSON and SQLite friendly values."""

    if value is None:
        return None
    try:
        missing = pd.isna(value)
        if isinstance(missing, bool) and missing:
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def parse_args() -> argparse.Namespace:
    """Parse realtime tick collector options."""

    parser = argparse.ArgumentParser(
        description="Collect moomoo realtime ticker rows into SQLite.",
    )
    parser.add_argument("--codes", default="", help="Comma-separated US./HK. symbols.")
    parser.add_argument("--markets", default="US,HK")
    parser.add_argument("--us-watchlist", default=DEFAULT_US_WATCHLIST)
    parser.add_argument("--hk-watchlist", default=DEFAULT_HK_WATCHLIST)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=11111)
    parser.add_argument("--duration-seconds", type=int, default=0)
    parser.add_argument("--cache-num", type=int, default=1000)
    parser.add_argument("--order-book-levels", type=int, default=50)
    parser.add_argument("--order-book-slippage-qty", type=float, default=1000.0)
    parser.add_argument("--dark-pool-us-min-notional", type=float, default=100_000.0)
    parser.add_argument("--dark-pool-hk-min-notional", type=float, default=800_000.0)
    parser.add_argument("--l2-imbalance-level", type=int, default=10)
    parser.add_argument("--l2-imbalance-warn", type=float, default=0.35)
    parser.add_argument("--l2-imbalance-danger", type=float, default=0.60)
    parser.add_argument("--broker-queue-interval", type=float, default=60.0)
    parser.add_argument("--quote-snapshot-interval", type=float, default=60.0)
    parser.add_argument("--quote-snapshot-batch-size", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument("--flush-interval", type=float, default=5.0)
    parser.add_argument("--subscribe-batch-size", type=int, default=50)
    parser.add_argument("--post-subscribe-wait", type=float, default=2.0)
    parser.add_argument(
        "--collect-ticks",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Collect realtime TICKER rows.",
    )
    parser.add_argument(
        "--collect-order-book",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Collect realtime ORDER_BOOK L2 snapshots.",
    )
    parser.add_argument(
        "--collect-broker-queue",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Collect HK realtime BROKER queue snapshots.",
    )
    parser.add_argument(
        "--collect-quote-snapshots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Collect low-frequency get_market_snapshot rows.",
    )
    parser.add_argument(
        "--init-db-only",
        action="store_true",
        help="Create tick tables and exit without connecting to OpenD.",
    )
    return parser.parse_args()


def load_watchlist(path: Path, prefix: str) -> tuple[str, ...]:
    """Load a watchlist file, preserving order and dropping duplicates."""

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


def parse_markets(raw: str) -> tuple[str, ...]:
    """Parse selected market labels."""

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


def explicit_codes(raw: str) -> tuple[str, ...]:
    """Parse explicit symbols from the CLI."""

    seen: set[str] = set()
    codes: list[str] = []
    for item in raw.split(","):
        code = item.strip()
        if code and code not in seen and (code.startswith("US.") or code.startswith("HK.")):
            seen.add(code)
            codes.append(code)
    return tuple(codes)


def market_for_code(code: str) -> str:
    """Return the supported market label for a moomoo security code."""

    if code.startswith("US."):
        return "US"
    if code.startswith("HK."):
        return "HK"
    raise ValueError(f"unsupported code prefix: {code}")


def market_configs(args: argparse.Namespace) -> dict[str, MarketConfig]:
    """Build market configs from CLI arguments."""

    return {
        "US": MarketConfig(
            label="US",
            prefix="US.",
            watchlist_path=Path(args.us_watchlist),
            timezone=US_TZ,
            session=ft.Session.ALL,
        ),
        "HK": MarketConfig(
            label="HK",
            prefix="HK.",
            watchlist_path=Path(args.hk_watchlist),
            timezone=HK_TZ,
            session=ft.Session.NONE,
        ),
    }


def resolve_codes(args: argparse.Namespace) -> tuple[str, ...]:
    """Resolve the final ordered symbol list from explicit codes or watchlists."""

    selected = parse_markets(args.markets)
    configs = market_configs(args)
    if args.codes.strip():
        codes = explicit_codes(args.codes)
        return tuple(code for code in codes if market_for_code(code) in selected)

    codes: list[str] = []
    for market in selected:
        cfg = configs[market]
        codes.extend(load_watchlist(cfg.watchlist_path, cfg.prefix))
    if not codes:
        raise ValueError("no selected tick symbols found")
    return tuple(codes)


def local_time_to_utc(raw_time: Any, market: str) -> str | None:
    """Convert a moomoo ticker time string into UTC ISO time."""

    if raw_time is None:
        return None
    text = str(raw_time).strip()
    if not text:
        return None
    parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    timestamp = pd.Timestamp(parsed)
    if timestamp.tzinfo is None:
        local_tz = US_TZ if market == "US" else HK_TZ
        timestamp = timestamp.tz_localize(local_tz)
    return timestamp.tz_convert(timezone.utc).isoformat()


def _trade_date_from_local_time(raw_time: Any) -> str | None:
    """Return the market-local trade date from a moomoo ticker time value."""

    if raw_time is None:
        return None
    parsed = pd.to_datetime(str(raw_time), errors="coerce")
    if pd.isna(parsed):
        return None
    return pd.Timestamp(parsed).date().isoformat()


def _trade_date_from_utc(raw_time: Any, market: str) -> str:
    """Return the market-local trade date from a UTC timestamp."""

    parsed = pd.to_datetime(str(raw_time), errors="coerce", utc=True)
    if pd.isna(parsed):
        return utc_now()[:10]
    target_tz = US_TZ if market == "US" else HK_TZ
    return pd.Timestamp(parsed).tz_convert(target_tz).date().isoformat()


def _float_or_zero(value: Any) -> float:
    """Return a finite float or zero."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _float_or_none(value: Any) -> float | None:
    """Return a finite float or None."""

    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _avg(values: list[float | None]) -> float | None:
    """Return the average of finite values."""

    clean = [value for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _max(values: list[float | None]) -> float | None:
    """Return the maximum of finite values."""

    clean = [value for value in values if value is not None]
    return max(clean) if clean else None


def _empty_daily_row(trade_date: str, code: str, market: str) -> dict[str, Any]:
    """Build a mutable daily microstructure aggregate row."""

    return {
        "trade_date": trade_date,
        "_code": code,
        "market": market,
        "dark_pool_event_count": 0,
        "dark_pool_buy_notional": 0.0,
        "dark_pool_sell_notional": 0.0,
        "dark_pool_unknown_notional": 0.0,
        "dark_pool_net_ratio": None,
        "dark_pool_score_max": None,
        "dark_pool_largest_notional": 0.0,
        "l2_snapshot_count": 0,
        "l2_score_avg": None,
        "l2_score_max": None,
        "l2_imbalance_avg": None,
        "l2_danger_count": 0,
        "spread_bps_avg": None,
        "spread_bps_max": None,
        "buy_slippage_bps_avg": None,
        "sell_slippage_bps_avg": None,
        "broker_snapshot_count": 0,
        "broker_score_avg": None,
        "broker_score_max": None,
        "broker_ask_ratio_avg": None,
        "broker_ask_volume_share_avg": None,
        "updated_at": utc_now(),
        "_dark_scores": [],
        "_l2_scores": [],
        "_l2_imbalances": [],
        "_spread_bps": [],
        "_buy_slippage_bps": [],
        "_sell_slippage_bps": [],
        "_broker_scores": [],
        "_broker_ask_ratios": [],
        "_broker_ask_volume_shares": [],
    }


def _finalize_daily_row(row: dict[str, Any]) -> dict[str, Any]:
    """Finalize a daily aggregate row for SQLite insertion."""

    buy = _float_or_zero(row["dark_pool_buy_notional"])
    sell = _float_or_zero(row["dark_pool_sell_notional"])
    directional_total = buy + sell
    row["dark_pool_net_ratio"] = (
        (buy - sell) / directional_total if directional_total > 0 else None
    )
    row["dark_pool_score_max"] = _max(row["_dark_scores"])
    row["l2_score_avg"] = _avg(row["_l2_scores"])
    row["l2_score_max"] = _max(row["_l2_scores"])
    row["l2_imbalance_avg"] = _avg(row["_l2_imbalances"])
    row["spread_bps_avg"] = _avg(row["_spread_bps"])
    row["spread_bps_max"] = _max(row["_spread_bps"])
    row["buy_slippage_bps_avg"] = _avg(row["_buy_slippage_bps"])
    row["sell_slippage_bps_avg"] = _avg(row["_sell_slippage_bps"])
    row["broker_score_avg"] = _avg(row["_broker_scores"])
    row["broker_score_max"] = _max(row["_broker_scores"])
    row["broker_ask_ratio_avg"] = _avg(row["_broker_ask_ratios"])
    row["broker_ask_volume_share_avg"] = _avg(row["_broker_ask_volume_shares"])
    row["updated_at"] = utc_now()
    return {
        key: value
        for key, value in row.items()
        if not key.startswith("_") or key == "_code"
    }


def _l2_signal_record(
    snapshot: dict[str, Any],
    signal: L2ImbalanceSignal,
    level: int,
    run_id: str,
    fetched_at: str,
) -> dict[str, Any]:
    """Build a SQLite row for one L2 imbalance signal."""

    market = str(snapshot.get("market") or "")
    snapshot_ts = str(snapshot.get("snapshot_ts_utc") or fetched_at)
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "_code": signal.code or str(snapshot.get("_code") or ""),
        "market": market,
        "trade_date": _trade_date_from_utc(snapshot_ts, market),
        "snapshot_ts_utc": snapshot_ts,
        "level": level,
        "imbalance": signal.imbalance,
        "score": signal.score,
        "direction": signal.direction,
        "risk_level": signal.risk_level,
        "reasons_json": json.dumps(
            list(signal.reasons),
            ensure_ascii=False,
            default=str,
        ),
        "metrics_json": json.dumps(
            signal.metrics,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        ),
        "consecutive_high_risk": signal.consecutive_high_risk,
        "should_alert": int(signal.should_alert),
        "_run_id": run_id,
        "_fetched_at": fetched_at,
    }


def _frame_records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    """Return normalized records from an optional pandas frame."""

    if frame is None or frame.empty:
        return []
    return [
        {str(key): normalize_value(value) for key, value in row.items()}
        for row in frame.where(pd.notnull(frame), None).to_dict("records")
    ]


def _broker_side_rows(
    *,
    snapshot_id: str,
    code: str,
    market: str,
    trade_date: str,
    snapshot_ts: str,
    side: str,
    records: list[dict[str, Any]],
    source: str,
    run_id: str,
    fetched_at: str,
) -> list[dict[str, Any]]:
    """Build normalized broker queue rows for one side."""

    prefix = "bid" if side == "BID" else "ask"
    rows: list[dict[str, Any]] = []
    for index, record in enumerate(records, start=1):
        rows.append(
            {
                "snapshot_id": snapshot_id,
                "_code": code,
                "market": market,
                "trade_date": trade_date,
                "snapshot_ts_utc": snapshot_ts,
                "side": side,
                "level": index,
                "broker_id": record.get(f"{prefix}_broker_id"),
                "broker_name": record.get(f"{prefix}_broker_name"),
                "broker_pos": record.get(f"{prefix}_broker_pos"),
                "order_id": record.get("order_id"),
                "order_volume": _float_or_none(record.get("order_volume")),
                "source": source,
                "_run_id": run_id,
                "_fetched_at": fetched_at,
            }
        )
    return rows


def _broker_top(
    records: list[dict[str, Any]],
    prefix: str,
    total_volume: float,
) -> tuple[Any, Any, float, float | None]:
    """Return top broker id/name/volume/share by order volume."""

    grouped: dict[str, tuple[Any, float]] = {}
    for record in records:
        broker_id = str(record.get(f"{prefix}_broker_id") or "")
        if not broker_id:
            continue
        broker_name = record.get(f"{prefix}_broker_name")
        volume = _float_or_zero(record.get("order_volume"))
        name, current = grouped.get(broker_id, (broker_name, 0.0))
        grouped[broker_id] = (name or broker_name, current + volume)
    if not grouped:
        return None, None, 0.0, None
    broker_id, (broker_name, volume) = max(
        grouped.items(),
        key=lambda item: item[1][1],
    )
    share = volume / total_volume if total_volume > 0 else None
    return broker_id, broker_name, volume, share


def _broker_queue_metric_record(
    *,
    snapshot_id: str,
    code: str,
    market: str,
    trade_date: str,
    snapshot_ts: str,
    bid_records: list[dict[str, Any]],
    ask_records: list[dict[str, Any]],
    run_id: str,
    fetched_at: str,
) -> dict[str, Any]:
    """Build one broker queue metric row."""

    bid_count = len(bid_records)
    ask_count = len(ask_records)
    bid_volume = sum(_float_or_zero(row.get("order_volume")) for row in bid_records)
    ask_volume = sum(_float_or_zero(row.get("order_volume")) for row in ask_records)
    count_total = bid_count + ask_count
    volume_total = bid_volume + ask_volume
    ask_ratio = ask_count / count_total if count_total > 0 else None
    ask_volume_share = ask_volume / volume_total if volume_total > 0 else None
    score = max(0.0, min(100.0, (ask_ratio if ask_ratio is not None else 0.5) * 100.0))
    bid_top = _broker_top(bid_records, "bid", bid_volume)
    ask_top = _broker_top(ask_records, "ask", ask_volume)
    metrics = {
        "bid_count": bid_count,
        "ask_count": ask_count,
        "bid_order_volume": bid_volume,
        "ask_order_volume": ask_volume,
        "ask_ratio": ask_ratio,
        "ask_volume_share": ask_volume_share,
        "score": score,
        "bid_top_broker_share": bid_top[3],
        "ask_top_broker_share": ask_top[3],
    }
    return {
        "snapshot_id": snapshot_id,
        "_code": code,
        "market": market,
        "trade_date": trade_date,
        "snapshot_ts_utc": snapshot_ts,
        "bid_count": bid_count,
        "ask_count": ask_count,
        "bid_order_volume": bid_volume,
        "ask_order_volume": ask_volume,
        "ask_ratio": ask_ratio,
        "ask_volume_share": ask_volume_share,
        "bid_top_broker_id": bid_top[0],
        "bid_top_broker_name": bid_top[1],
        "bid_top_broker_volume": bid_top[2],
        "bid_top_broker_share": bid_top[3],
        "ask_top_broker_id": ask_top[0],
        "ask_top_broker_name": ask_top[1],
        "ask_top_broker_volume": ask_top[2],
        "ask_top_broker_share": ask_top[3],
        "score": score,
        "metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
        "_run_id": run_id,
        "_fetched_at": fetched_at,
    }


def frame_to_records(
    frame: pd.DataFrame,
    source: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Convert a moomoo ticker DataFrame into normalized DB records."""

    if frame is None or frame.empty:
        return []

    now = utc_now()
    records: list[dict[str, Any]] = []
    rows = frame.where(pd.notnull(frame), None).to_dict("records")
    for row in rows:
        normalized = {str(key): normalize_value(value) for key, value in row.items()}
        code = str(normalized.get("code") or "")
        sequence = normalize_value(normalized.get("sequence"))
        if not code or sequence is None:
            continue
        market = market_for_code(code)
        payload = json.dumps(
            normalized,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        records.append(
            {
                "_code": code,
                "market": market,
                "name": normalized.get("name"),
                "time": normalized.get("time"),
                "ts_utc": local_time_to_utc(normalized.get("time"), market),
                "sequence": str(sequence),
                "price": normalized.get("price"),
                "volume": normalized.get("volume"),
                "turnover": normalized.get("turnover"),
                "ticker_direction": normalized.get("ticker_direction"),
                "type": normalized.get("type"),
                "type_sign": normalized.get("type_sign"),
                "push_data_type": normalized.get("push_data_type"),
                "recv_time": normalized.get("recv_time"),
                "timestamp": normalized.get("timestamp"),
                "hp_volume": normalized.get("hp_volume"),
                "source": source,
                "_run_id": run_id,
                "_fetched_at": now,
                "_payload_json": payload,
            }
        )
    return records


def quote_snapshot_records(
    frame: pd.DataFrame,
    source: str,
    run_id: str,
) -> list[dict[str, Any]]:
    """Convert get_market_snapshot rows into low-frequency quote snapshots."""

    now = utc_now()
    records: list[dict[str, Any]] = []
    for row in _frame_records(frame):
        code = str(row.get("code") or "")
        if not code:
            continue
        try:
            market = market_for_code(code)
        except ValueError:
            continue
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        records.append(
            {
                "snapshot_id": uuid.uuid4().hex,
                "_code": code,
                "market": market,
                "trade_date": _trade_date_from_utc(now, market),
                "snapshot_ts_utc": now,
                "source": source,
                "name": row.get("name"),
                "last_price": _float_or_none(row.get("last_price")),
                "cur_price": _float_or_none(row.get("cur_price")),
                "bid_price": _float_or_none(row.get("bid_price")),
                "ask_price": _float_or_none(row.get("ask_price")),
                "bid_vol": _float_or_none(row.get("bid_vol")),
                "ask_vol": _float_or_none(row.get("ask_vol")),
                "volume": _float_or_none(row.get("volume")),
                "turnover": _float_or_none(row.get("turnover")),
                "turnover_rate": _float_or_none(row.get("turnover_rate")),
                "open_price": _float_or_none(row.get("open_price")),
                "high_price": _float_or_none(row.get("high_price")),
                "low_price": _float_or_none(row.get("low_price")),
                "prev_close_price": _float_or_none(row.get("prev_close_price")),
                "market_status": row.get("market_status"),
                "update_time": row.get("update_time"),
                "lot_size": _float_or_none(row.get("lot_size")),
                "price_spread": _float_or_none(row.get("price_spread")),
                "dark_status": row.get("dark_status"),
                "sec_status": row.get("sec_status"),
                "_run_id": run_id,
                "_fetched_at": now,
                "_payload_json": payload,
            }
        )
    return records


def chunked(items: tuple[str, ...], size: int) -> Iterable[tuple[str, ...]]:
    """Yield fixed-size chunks."""

    if size <= 0:
        raise ValueError("batch size must be positive")
    for start in range(0, len(items), size):
        yield items[start : start + size]


class TickStore:
    """SQLite storage for realtime ticker rows and run audit records."""

    def __init__(self, db_path: Path, run_id: str) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.lock = threading.Lock()
        self.rows_written = 0
        self.cache_rows = 0
        self.push_rows = 0
        self.order_book_snapshots = 0
        self.order_book_levels = 0
        self.order_book_metric_rows = 0
        self.quote_snapshots = 0
        self.broker_queue_snapshots = 0
        self.broker_queue_levels = 0
        self.broker_queue_metric_rows = 0
        self.dark_pool_proxy_events = 0
        self.dark_pool_proxy_metric_rows = 0
        self.l2_imbalance_signal_rows = 0
        self.microstructure_alerts = 0
        self.microstructure_daily_feature_rows = 0
        self._init_schema()

    def close(self) -> None:
        """Flush and close the SQLite connection."""

        with self.lock:
            self.conn.commit()
            self.conn.close()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tick_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                markets_json TEXT NOT NULL,
                codes_json TEXT NOT NULL,
                status TEXT NOT NULL,
                rows_written INTEGER NOT NULL DEFAULT 0,
                cache_rows INTEGER NOT NULL DEFAULT 0,
                push_rows INTEGER NOT NULL DEFAULT 0,
                order_book_snapshots INTEGER NOT NULL DEFAULT 0,
                order_book_levels INTEGER NOT NULL DEFAULT 0,
                order_book_metric_rows INTEGER NOT NULL DEFAULT 0,
                quote_snapshots INTEGER NOT NULL DEFAULT 0,
                broker_queue_snapshots INTEGER NOT NULL DEFAULT 0,
                broker_queue_levels INTEGER NOT NULL DEFAULT 0,
                broker_queue_metric_rows INTEGER NOT NULL DEFAULT 0,
                dark_pool_proxy_events INTEGER NOT NULL DEFAULT 0,
                dark_pool_proxy_metric_rows INTEGER NOT NULL DEFAULT 0,
                l2_imbalance_signal_rows INTEGER NOT NULL DEFAULT 0,
                microstructure_alerts INTEGER NOT NULL DEFAULT 0,
                microstructure_daily_feature_rows INTEGER NOT NULL DEFAULT 0,
                note TEXT
            )
            """
        )
        self._ensure_columns(
            "tick_runs",
            {
                "order_book_snapshots": "INTEGER NOT NULL DEFAULT 0",
                "order_book_levels": "INTEGER NOT NULL DEFAULT 0",
                "order_book_metric_rows": "INTEGER NOT NULL DEFAULT 0",
                "quote_snapshots": "INTEGER NOT NULL DEFAULT 0",
                "broker_queue_snapshots": "INTEGER NOT NULL DEFAULT 0",
                "broker_queue_levels": "INTEGER NOT NULL DEFAULT 0",
                "broker_queue_metric_rows": "INTEGER NOT NULL DEFAULT 0",
                "dark_pool_proxy_events": "INTEGER NOT NULL DEFAULT 0",
                "dark_pool_proxy_metric_rows": "INTEGER NOT NULL DEFAULT 0",
                "l2_imbalance_signal_rows": "INTEGER NOT NULL DEFAULT 0",
                "microstructure_alerts": "INTEGER NOT NULL DEFAULT 0",
                "microstructure_daily_feature_rows": "INTEGER NOT NULL DEFAULT 0",
            },
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tick_errors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                code TEXT,
                stage TEXT NOT NULL,
                message TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dark_pool_proxy_events (
                _code TEXT NOT NULL,
                sequence TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                event_time TEXT,
                price REAL NOT NULL,
                volume REAL NOT NULL,
                notional REAL NOT NULL,
                direction TEXT NOT NULL,
                threshold REAL NOT NULL,
                source TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL,
                PRIMARY KEY (_code, sequence, trade_date)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dark_pool_proxy_metrics (
                metric_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                metric_ts_utc TEXT NOT NULL,
                threshold REAL NOT NULL,
                print_count INTEGER NOT NULL,
                buy_count INTEGER NOT NULL,
                sell_count INTEGER NOT NULL,
                unknown_count INTEGER NOT NULL,
                total_notional REAL NOT NULL,
                buy_notional REAL NOT NULL,
                sell_notional REAL NOT NULL,
                unknown_notional REAL NOT NULL,
                largest_notional REAL NOT NULL,
                net_ratio REAL,
                score REAL NOT NULL,
                risk_level TEXT NOT NULL,
                latest_time TEXT,
                prints_json TEXT NOT NULL,
                source TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS l2_imbalance_signals (
                snapshot_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                level INTEGER NOT NULL,
                imbalance REAL,
                score REAL NOT NULL,
                direction TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                metrics_json TEXT NOT NULL,
                consecutive_high_risk INTEGER NOT NULL,
                should_alert INTEGER NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broker_queue_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                bid_row_count INTEGER NOT NULL,
                ask_row_count INTEGER NOT NULL,
                bid_payload_json TEXT NOT NULL,
                ask_payload_json TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broker_queue_levels (
                snapshot_id TEXT NOT NULL,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                side TEXT NOT NULL,
                level INTEGER NOT NULL,
                broker_id TEXT,
                broker_name TEXT,
                broker_pos INTEGER,
                order_id TEXT,
                order_volume REAL,
                source TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, side, level)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broker_queue_metrics (
                snapshot_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                bid_count INTEGER NOT NULL,
                ask_count INTEGER NOT NULL,
                bid_order_volume REAL NOT NULL,
                ask_order_volume REAL NOT NULL,
                ask_ratio REAL,
                ask_volume_share REAL,
                bid_top_broker_id TEXT,
                bid_top_broker_name TEXT,
                bid_top_broker_volume REAL,
                bid_top_broker_share REAL,
                ask_top_broker_id TEXT,
                ask_top_broker_name TEXT,
                ask_top_broker_volume REAL,
                ask_top_broker_share REAL,
                score REAL NOT NULL,
                metrics_json TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS microstructure_alerts (
                alert_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                ts_utc TEXT NOT NULL,
                score REAL NOT NULL,
                risk_level TEXT NOT NULL,
                message TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS microstructure_daily_features (
                trade_date TEXT NOT NULL,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                dark_pool_event_count INTEGER NOT NULL DEFAULT 0,
                dark_pool_buy_notional REAL NOT NULL DEFAULT 0,
                dark_pool_sell_notional REAL NOT NULL DEFAULT 0,
                dark_pool_unknown_notional REAL NOT NULL DEFAULT 0,
                dark_pool_net_ratio REAL,
                dark_pool_score_max REAL,
                dark_pool_largest_notional REAL NOT NULL DEFAULT 0,
                l2_snapshot_count INTEGER NOT NULL DEFAULT 0,
                l2_score_avg REAL,
                l2_score_max REAL,
                l2_imbalance_avg REAL,
                l2_danger_count INTEGER NOT NULL DEFAULT 0,
                spread_bps_avg REAL,
                spread_bps_max REAL,
                buy_slippage_bps_avg REAL,
                sell_slippage_bps_avg REAL,
                broker_snapshot_count INTEGER NOT NULL DEFAULT 0,
                broker_score_avg REAL,
                broker_score_max REAL,
                broker_ask_ratio_avg REAL,
                broker_ask_volume_share_avg REAL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (trade_date, _code)
            )
            """
        )
        self._ensure_columns(
            "microstructure_daily_features",
            {
                "broker_snapshot_count": "INTEGER NOT NULL DEFAULT 0",
                "broker_score_avg": "REAL",
                "broker_score_max": "REAL",
                "broker_ask_ratio_avg": "REAL",
                "broker_ask_volume_share_avg": "REAL",
            },
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_quote_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                trade_date TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                name TEXT,
                last_price REAL,
                cur_price REAL,
                bid_price REAL,
                ask_price REAL,
                bid_vol REAL,
                ask_vol REAL,
                volume REAL,
                turnover REAL,
                turnover_rate REAL,
                open_price REAL,
                high_price REAL,
                low_price REAL,
                prev_close_price REAL,
                market_status TEXT,
                update_time TEXT,
                lot_size REAL,
                price_spread REAL,
                dark_status TEXT,
                sec_status TEXT,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL,
                _payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS realtime_ticks (
                _code TEXT NOT NULL,
                sequence TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT,
                time TEXT,
                ts_utc TEXT,
                price REAL,
                volume REAL,
                turnover REAL,
                ticker_direction TEXT,
                type TEXT,
                type_sign TEXT,
                push_data_type TEXT,
                recv_time REAL,
                timestamp REAL,
                hp_volume REAL,
                source TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL,
                _payload_json TEXT NOT NULL,
                PRIMARY KEY (_code, sequence)
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_dark_pool_proxy_events_code_date
              ON dark_pool_proxy_events (_code, trade_date)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_l2_imbalance_signals_code_date
              ON l2_imbalance_signals (_code, trade_date)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_broker_queue_metrics_code_date
              ON broker_queue_metrics (_code, trade_date)
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_book_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT,
                snapshot_ts_utc TEXT NOT NULL,
                source TEXT NOT NULL,
                bid_svr_recv_time TEXT,
                ask_svr_recv_time TEXT,
                bid_svr_recv_time_timestamp REAL,
                ask_svr_recv_time_timestamp REAL,
                order_book_type TEXT,
                bid_level_count INTEGER NOT NULL,
                ask_level_count INTEGER NOT NULL,
                bid_levels_json TEXT NOT NULL,
                ask_levels_json TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL,
                _payload_json TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_book_levels (
                snapshot_id TEXT NOT NULL,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                side TEXT NOT NULL,
                level INTEGER NOT NULL,
                price REAL NOT NULL,
                volume REAL NOT NULL,
                order_count INTEGER,
                detail_json TEXT NOT NULL,
                source TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                PRIMARY KEY (snapshot_id, side, level)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS order_book_metrics (
                snapshot_id TEXT PRIMARY KEY,
                _code TEXT NOT NULL,
                market TEXT NOT NULL,
                snapshot_ts_utc TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                mid_price REAL,
                spread REAL,
                spread_bps REAL,
                micro_price REAL,
                bid_depth_1 REAL,
                ask_depth_1 REAL,
                imbalance_1 REAL,
                bid_depth_5 REAL,
                ask_depth_5 REAL,
                imbalance_5 REAL,
                bid_depth_10 REAL,
                ask_depth_10 REAL,
                imbalance_10 REAL,
                bid_depth_50 REAL,
                ask_depth_50 REAL,
                imbalance_50 REAL,
                depth_change_rate REAL,
                estimated_buy_slippage_bps REAL,
                estimated_sell_slippage_bps REAL,
                metrics_json TEXT NOT NULL,
                _run_id TEXT NOT NULL,
                _fetched_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_realtime_ticks_market_time
              ON realtime_ticks (market, ts_utc)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_realtime_ticks_run
              ON realtime_ticks (_run_id)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_realtime_quote_snapshots_code_time
              ON realtime_quote_snapshots (_code, snapshot_ts_utc)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_realtime_quote_snapshots_market_time
              ON realtime_quote_snapshots (market, snapshot_ts_utc)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_realtime_quote_snapshots_code_date
              ON realtime_quote_snapshots (_code, trade_date)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_code_time
              ON order_book_snapshots (_code, snapshot_ts_utc)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_book_snapshots_market_time
              ON order_book_snapshots (market, snapshot_ts_utc)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_order_book_metrics_code_time
              ON order_book_metrics (_code, snapshot_ts_utc)
            """
        )
        self.conn.commit()

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        """Add missing columns for existing SQLite databases."""

        existing = {
            str(row[1])
            for row in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for column, definition in columns.items():
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def start_run(self, markets: tuple[str, ...], codes: tuple[str, ...]) -> None:
        """Insert a running tick collection audit row."""

        with self.lock:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO tick_runs
                    (run_id, started_at, markets_json, codes_json, status, note)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    self.run_id,
                    utc_now(),
                    json.dumps(markets, ensure_ascii=False),
                    json.dumps(codes, ensure_ascii=False),
                    "running",
                    "read-only moomoo realtime tick and L2 order book collection",
                ),
            )
            self.conn.commit()

    def finish_run(self, status: str, note: str | None = None) -> None:
        """Mark the tick collection run as finished."""

        with self.lock:
            self.conn.execute(
                """
                UPDATE tick_runs
                   SET finished_at = ?,
                       status = ?,
                       rows_written = ?,
                       cache_rows = ?,
                       push_rows = ?,
                       order_book_snapshots = ?,
                       order_book_levels = ?,
                       order_book_metric_rows = ?,
                       quote_snapshots = ?,
                       broker_queue_snapshots = ?,
                       broker_queue_levels = ?,
                       broker_queue_metric_rows = ?,
                       dark_pool_proxy_events = ?,
                       dark_pool_proxy_metric_rows = ?,
                       l2_imbalance_signal_rows = ?,
                       microstructure_alerts = ?,
                       microstructure_daily_feature_rows = ?,
                       note = COALESCE(?, note)
                 WHERE run_id = ?
                """,
                (
                    utc_now(),
                    status,
                    self.rows_written,
                    self.cache_rows,
                    self.push_rows,
                    self.order_book_snapshots,
                    self.order_book_levels,
                    self.order_book_metric_rows,
                    self.quote_snapshots,
                    self.broker_queue_snapshots,
                    self.broker_queue_levels,
                    self.broker_queue_metric_rows,
                    self.dark_pool_proxy_events,
                    self.dark_pool_proxy_metric_rows,
                    self.l2_imbalance_signal_rows,
                    self.microstructure_alerts,
                    self.microstructure_daily_feature_rows,
                    note,
                    self.run_id,
                ),
            )
            self.conn.commit()

    def log_error(self, code: str | None, stage: str, message: str) -> None:
        """Record an operational error for later audit."""

        with self.lock:
            self.conn.execute(
                """
                INSERT INTO tick_errors (run_id, ts, code, stage, message)
                VALUES (?, ?, ?, ?, ?)
                """,
                (self.run_id, utc_now(), code, stage, message[:2000]),
            )
            self.conn.commit()

    def insert_records(self, records: list[dict[str, Any]]) -> int:
        """Upsert normalized ticker records."""

        if not records:
            return 0
        columns = (
            "_code",
            "sequence",
            "market",
            "name",
            "time",
            "ts_utc",
            "price",
            "volume",
            "turnover",
            "ticker_direction",
            "type",
            "type_sign",
            "push_data_type",
            "recv_time",
            "timestamp",
            "hp_volume",
            "source",
            "_run_id",
            "_fetched_at",
            "_payload_json",
        )
        placeholders = ", ".join("?" for _ in columns)
        set_clause = ", ".join(
            f"{column} = excluded.{column}"
            for column in columns
            if column not in {"_code", "sequence"}
        )
        sql = (
            "INSERT INTO realtime_ticks "
            f"({', '.join(columns)}) VALUES ({placeholders}) "
            f"ON CONFLICT(_code, sequence) DO UPDATE SET {set_clause}"
        )
        values = [[record.get(column) for column in columns] for record in records]
        with self.lock:
            before = self.conn.total_changes
            self.conn.executemany(sql, values)
            self.conn.commit()
            changed = self.conn.total_changes - before
            self.rows_written += len(records)
            self.cache_rows += sum(1 for record in records if record["source"] == "cache")
            self.push_rows += sum(1 for record in records if record["source"] == "push")
        return changed

    def insert_quote_snapshot_records(
        self,
        frame: pd.DataFrame,
        source: str,
    ) -> int:
        """Persist low-frequency get_market_snapshot rows."""

        records = quote_snapshot_records(frame, source, self.run_id)
        if not records:
            return 0
        columns = (
            "snapshot_id",
            "_code",
            "market",
            "trade_date",
            "snapshot_ts_utc",
            "source",
            "name",
            "last_price",
            "cur_price",
            "bid_price",
            "ask_price",
            "bid_vol",
            "ask_vol",
            "volume",
            "turnover",
            "turnover_rate",
            "open_price",
            "high_price",
            "low_price",
            "prev_close_price",
            "market_status",
            "update_time",
            "lot_size",
            "price_spread",
            "dark_status",
            "sec_status",
            "_run_id",
            "_fetched_at",
            "_payload_json",
        )
        with self.lock:
            self._insert_or_replace("realtime_quote_snapshots", columns, records)
            self.quote_snapshots += len(records)
        return len(records)

    def insert_order_book_records(
        self,
        snapshot: dict[str, Any],
        levels: list[dict[str, Any]],
        metrics: dict[str, Any],
    ) -> None:
        """Upsert one normalized order book snapshot and its derived rows."""

        snapshot_columns = (
            "snapshot_id",
            "_code",
            "market",
            "name",
            "snapshot_ts_utc",
            "source",
            "bid_svr_recv_time",
            "ask_svr_recv_time",
            "bid_svr_recv_time_timestamp",
            "ask_svr_recv_time_timestamp",
            "order_book_type",
            "bid_level_count",
            "ask_level_count",
            "bid_levels_json",
            "ask_levels_json",
            "_run_id",
            "_fetched_at",
            "_payload_json",
        )
        level_columns = (
            "snapshot_id",
            "_code",
            "market",
            "snapshot_ts_utc",
            "side",
            "level",
            "price",
            "volume",
            "order_count",
            "detail_json",
            "source",
            "_run_id",
        )
        metric_columns = (
            "snapshot_id",
            "_code",
            "market",
            "snapshot_ts_utc",
            "best_bid",
            "best_ask",
            "mid_price",
            "spread",
            "spread_bps",
            "micro_price",
            "bid_depth_1",
            "ask_depth_1",
            "imbalance_1",
            "bid_depth_5",
            "ask_depth_5",
            "imbalance_5",
            "bid_depth_10",
            "ask_depth_10",
            "imbalance_10",
            "bid_depth_50",
            "ask_depth_50",
            "imbalance_50",
            "depth_change_rate",
            "estimated_buy_slippage_bps",
            "estimated_sell_slippage_bps",
            "metrics_json",
            "_run_id",
            "_fetched_at",
        )
        with self.lock:
            self._insert_or_replace("order_book_snapshots", snapshot_columns, [snapshot])
            self._insert_or_replace("order_book_levels", level_columns, levels)
            self._insert_or_replace("order_book_metrics", metric_columns, [metrics])
            self.order_book_snapshots += 1
            self.order_book_levels += len(levels)
            self.order_book_metric_rows += 1

    def insert_broker_queue_records(
        self,
        code: str,
        bid_frame: pd.DataFrame,
        ask_frame: pd.DataFrame,
        source: str,
    ) -> None:
        """Persist one HK broker queue snapshot and derived metrics."""

        if market_for_code(code) != "HK":
            return
        fetched_at = utc_now()
        snapshot_id = uuid.uuid4().hex
        market = "HK"
        trade_date = _trade_date_from_utc(fetched_at, market)
        bid_records = _frame_records(bid_frame)
        ask_records = _frame_records(ask_frame)
        snapshot = {
            "snapshot_id": snapshot_id,
            "_code": code,
            "market": market,
            "trade_date": trade_date,
            "snapshot_ts_utc": fetched_at,
            "source": source,
            "bid_row_count": len(bid_records),
            "ask_row_count": len(ask_records),
            "bid_payload_json": json.dumps(
                bid_records,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            "ask_payload_json": json.dumps(
                ask_records,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            "_run_id": self.run_id,
            "_fetched_at": fetched_at,
        }
        levels = [
            *_broker_side_rows(
                snapshot_id=snapshot_id,
                code=code,
                market=market,
                trade_date=trade_date,
                snapshot_ts=fetched_at,
                side="BID",
                records=bid_records,
                source=source,
                run_id=self.run_id,
                fetched_at=fetched_at,
            ),
            *_broker_side_rows(
                snapshot_id=snapshot_id,
                code=code,
                market=market,
                trade_date=trade_date,
                snapshot_ts=fetched_at,
                side="ASK",
                records=ask_records,
                source=source,
                run_id=self.run_id,
                fetched_at=fetched_at,
            ),
        ]
        metrics = _broker_queue_metric_record(
            snapshot_id=snapshot_id,
            code=code,
            market=market,
            trade_date=trade_date,
            snapshot_ts=fetched_at,
            bid_records=bid_records,
            ask_records=ask_records,
            run_id=self.run_id,
            fetched_at=fetched_at,
        )
        with self.lock:
            self._insert_or_replace(
                "broker_queue_snapshots",
                (
                    "snapshot_id",
                    "_code",
                    "market",
                    "trade_date",
                    "snapshot_ts_utc",
                    "source",
                    "bid_row_count",
                    "ask_row_count",
                    "bid_payload_json",
                    "ask_payload_json",
                    "_run_id",
                    "_fetched_at",
                ),
                [snapshot],
            )
            self._insert_or_replace(
                "broker_queue_levels",
                (
                    "snapshot_id",
                    "_code",
                    "market",
                    "trade_date",
                    "snapshot_ts_utc",
                    "side",
                    "level",
                    "broker_id",
                    "broker_name",
                    "broker_pos",
                    "order_id",
                    "order_volume",
                    "source",
                    "_run_id",
                    "_fetched_at",
                ),
                levels,
            )
            self._insert_or_replace(
                "broker_queue_metrics",
                (
                    "snapshot_id",
                    "_code",
                    "market",
                    "trade_date",
                    "snapshot_ts_utc",
                    "bid_count",
                    "ask_count",
                    "bid_order_volume",
                    "ask_order_volume",
                    "ask_ratio",
                    "ask_volume_share",
                    "bid_top_broker_id",
                    "bid_top_broker_name",
                    "bid_top_broker_volume",
                    "bid_top_broker_share",
                    "ask_top_broker_id",
                    "ask_top_broker_name",
                    "ask_top_broker_volume",
                    "ask_top_broker_share",
                    "score",
                    "metrics_json",
                    "_run_id",
                    "_fetched_at",
                ),
                [metrics],
            )
            self.broker_queue_snapshots += 1
            self.broker_queue_levels += len(levels)
            self.broker_queue_metric_rows += 1

    def insert_dark_pool_proxy_metrics(
        self,
        metrics: list[DarkPoolProxyMetrics],
        source: str,
    ) -> None:
        """Persist large-print proxy events and per-batch metrics."""

        if not metrics:
            return
        fetched_at = utc_now()
        event_rows: list[dict[str, Any]] = []
        metric_rows: list[dict[str, Any]] = []
        alert_rows: list[dict[str, Any]] = []
        for item in metrics:
            trade_date = _trade_date_from_local_time(item.latest_time) or ""
            for print_item in item.prints:
                event_trade_date = (
                    _trade_date_from_local_time(print_item.time) or trade_date
                )
                event_rows.append(
                    {
                        "_code": print_item.code,
                        "sequence": print_item.sequence,
                        "market": print_item.market,
                        "trade_date": event_trade_date,
                        "event_time": print_item.time,
                        "price": print_item.price,
                        "volume": print_item.volume,
                        "notional": print_item.notional,
                        "direction": print_item.direction,
                        "threshold": item.threshold,
                        "source": source,
                        "_run_id": self.run_id,
                        "_fetched_at": fetched_at,
                    }
                )
            metric_rows.append(
                {
                    "metric_id": uuid.uuid4().hex,
                    "_code": item.code,
                    "market": item.market,
                    "trade_date": trade_date,
                    "metric_ts_utc": fetched_at,
                    "threshold": item.threshold,
                    "print_count": item.print_count,
                    "buy_count": item.buy_count,
                    "sell_count": item.sell_count,
                    "unknown_count": item.unknown_count,
                    "total_notional": item.total_notional,
                    "buy_notional": item.buy_notional,
                    "sell_notional": item.sell_notional,
                    "unknown_notional": item.unknown_notional,
                    "largest_notional": item.largest_notional,
                    "net_ratio": item.net_ratio,
                    "score": item.score,
                    "risk_level": item.risk_level,
                    "latest_time": item.latest_time,
                    "prints_json": json.dumps(
                        [print_item.as_dict() for print_item in item.prints],
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    "source": source,
                    "_run_id": self.run_id,
                    "_fetched_at": fetched_at,
                }
            )
            if item.should_alert:
                alert_rows.append(
                    self._alert_row(
                        event_type="dark_pool_proxy",
                        code=item.code,
                        market=item.market,
                        trade_date=trade_date,
                        score=item.score,
                        risk_level=item.risk_level,
                        message=item.alert_message(),
                        payload=item.as_dict(),
                        ts_utc=fetched_at,
                        fetched_at=fetched_at,
                    )
                )
        with self.lock:
            self._insert_or_replace(
                "dark_pool_proxy_events",
                (
                    "_code",
                    "sequence",
                    "market",
                    "trade_date",
                    "event_time",
                    "price",
                    "volume",
                    "notional",
                    "direction",
                    "threshold",
                    "source",
                    "_run_id",
                    "_fetched_at",
                ),
                event_rows,
            )
            self._insert_or_replace(
                "dark_pool_proxy_metrics",
                (
                    "metric_id",
                    "_code",
                    "market",
                    "trade_date",
                    "metric_ts_utc",
                    "threshold",
                    "print_count",
                    "buy_count",
                    "sell_count",
                    "unknown_count",
                    "total_notional",
                    "buy_notional",
                    "sell_notional",
                    "unknown_notional",
                    "largest_notional",
                    "net_ratio",
                    "score",
                    "risk_level",
                    "latest_time",
                    "prints_json",
                    "source",
                    "_run_id",
                    "_fetched_at",
                ),
                metric_rows,
            )
            self._insert_or_replace(
                "microstructure_alerts",
                (
                    "alert_id",
                    "event_type",
                    "_code",
                    "market",
                    "trade_date",
                    "ts_utc",
                    "score",
                    "risk_level",
                    "message",
                    "payload_json",
                    "_run_id",
                    "_fetched_at",
                ),
                alert_rows,
            )
            self.dark_pool_proxy_events += len(event_rows)
            self.dark_pool_proxy_metric_rows += len(metric_rows)
            self.microstructure_alerts += len(alert_rows)

    def insert_l2_imbalance_signal(self, signal: dict[str, Any]) -> None:
        """Persist one L2 imbalance signal row and optional alert."""

        alert_rows: list[dict[str, Any]] = []
        if signal.get("should_alert"):
            alert_rows.append(
                self._alert_row(
                    event_type="l2_imbalance",
                    code=str(signal["_code"]),
                    market=str(signal["market"]),
                    trade_date=str(signal["trade_date"]),
                    score=float(signal["score"]),
                    risk_level=str(signal["risk_level"]),
                    message=(
                        f"{signal['_code']} l2_imbalance "
                        f"score={float(signal['score']):.1f} "
                        f"imbalance={signal.get('imbalance')}"
                    ),
                    payload=signal,
                    ts_utc=str(signal["snapshot_ts_utc"]),
                    fetched_at=str(signal["_fetched_at"]),
                )
            )
        with self.lock:
            self._insert_or_replace(
                "l2_imbalance_signals",
                (
                    "snapshot_id",
                    "_code",
                    "market",
                    "trade_date",
                    "snapshot_ts_utc",
                    "level",
                    "imbalance",
                    "score",
                    "direction",
                    "risk_level",
                    "reasons_json",
                    "metrics_json",
                    "consecutive_high_risk",
                    "should_alert",
                    "_run_id",
                    "_fetched_at",
                ),
                [signal],
            )
            self._insert_or_replace(
                "microstructure_alerts",
                (
                    "alert_id",
                    "event_type",
                    "_code",
                    "market",
                    "trade_date",
                    "ts_utc",
                    "score",
                    "risk_level",
                    "message",
                    "payload_json",
                    "_run_id",
                    "_fetched_at",
                ),
                alert_rows,
            )
            self.l2_imbalance_signal_rows += 1
            self.microstructure_alerts += len(alert_rows)

    def rebuild_microstructure_daily_features(self) -> None:
        """Rebuild daily aggregate microstructure features from derived tables."""

        rows: dict[tuple[str, str], dict[str, Any]] = {}
        with self.lock:
            dark_rows = self.conn.execute(
                """
                SELECT trade_date, _code, market, direction, notional
                  FROM dark_pool_proxy_events
                """
            ).fetchall()
            dark_metric_rows = self.conn.execute(
                """
                SELECT trade_date, _code, market, score
                  FROM dark_pool_proxy_metrics
                """
            ).fetchall()
            l2_rows = self.conn.execute(
                """
                SELECT trade_date, _code, market, score, imbalance, risk_level, metrics_json
                  FROM l2_imbalance_signals
                """
            ).fetchall()
            broker_rows = self.conn.execute(
                """
                SELECT trade_date, _code, market, score, ask_ratio, ask_volume_share
                  FROM broker_queue_metrics
                """
            ).fetchall()
            for trade_date, code, market, direction, notional in dark_rows:
                row = rows.setdefault(
                    (str(trade_date), str(code)),
                    _empty_daily_row(str(trade_date), str(code), str(market)),
                )
                row["dark_pool_event_count"] += 1
                value = _float_or_zero(notional)
                if str(direction) == "BUY":
                    row["dark_pool_buy_notional"] += value
                elif str(direction) == "SELL":
                    row["dark_pool_sell_notional"] += value
                else:
                    row["dark_pool_unknown_notional"] += value
                row["dark_pool_largest_notional"] = max(
                    row["dark_pool_largest_notional"],
                    value,
                )
            for trade_date, code, market, score in dark_metric_rows:
                row = rows.setdefault(
                    (str(trade_date), str(code)),
                    _empty_daily_row(str(trade_date), str(code), str(market)),
                )
                row["_dark_scores"].append(_float_or_none(score))
            for trade_date, code, market, score, imbalance, risk_level, metrics_json in l2_rows:
                row = rows.setdefault(
                    (str(trade_date), str(code)),
                    _empty_daily_row(str(trade_date), str(code), str(market)),
                )
                row["l2_snapshot_count"] += 1
                row["_l2_scores"].append(_float_or_none(score))
                row["_l2_imbalances"].append(_float_or_none(imbalance))
                if str(risk_level) == "danger":
                    row["l2_danger_count"] += 1
                try:
                    metrics = json.loads(str(metrics_json or "{}"))
                except json.JSONDecodeError:
                    metrics = {}
                row["_spread_bps"].append(_float_or_none(metrics.get("spread_bps")))
                row["_buy_slippage_bps"].append(
                    _float_or_none(metrics.get("estimated_buy_slippage_bps"))
                )
                row["_sell_slippage_bps"].append(
                    _float_or_none(metrics.get("estimated_sell_slippage_bps"))
                )
            for trade_date, code, market, score, ask_ratio, ask_volume_share in broker_rows:
                row = rows.setdefault(
                    (str(trade_date), str(code)),
                    _empty_daily_row(str(trade_date), str(code), str(market)),
                )
                row["broker_snapshot_count"] += 1
                row["_broker_scores"].append(_float_or_none(score))
                row["_broker_ask_ratios"].append(_float_or_none(ask_ratio))
                row["_broker_ask_volume_shares"].append(
                    _float_or_none(ask_volume_share)
                )
            final_rows = [_finalize_daily_row(row) for row in rows.values()]
            self.conn.execute("DELETE FROM microstructure_daily_features")
            self._insert_or_replace(
                "microstructure_daily_features",
                (
                    "trade_date",
                    "_code",
                    "market",
                    "dark_pool_event_count",
                    "dark_pool_buy_notional",
                    "dark_pool_sell_notional",
                    "dark_pool_unknown_notional",
                    "dark_pool_net_ratio",
                    "dark_pool_score_max",
                    "dark_pool_largest_notional",
                    "l2_snapshot_count",
                    "l2_score_avg",
                    "l2_score_max",
                    "l2_imbalance_avg",
                    "l2_danger_count",
                    "spread_bps_avg",
                    "spread_bps_max",
                    "buy_slippage_bps_avg",
                    "sell_slippage_bps_avg",
                    "broker_snapshot_count",
                    "broker_score_avg",
                    "broker_score_max",
                    "broker_ask_ratio_avg",
                    "broker_ask_volume_share_avg",
                    "updated_at",
                ),
                final_rows,
            )
            self.microstructure_daily_feature_rows = len(final_rows)

    def _alert_row(
        self,
        *,
        event_type: str,
        code: str,
        market: str,
        trade_date: str,
        score: float,
        risk_level: str,
        message: str,
        payload: dict[str, Any],
        ts_utc: str,
        fetched_at: str,
    ) -> dict[str, Any]:
        return {
            "alert_id": uuid.uuid4().hex,
            "event_type": event_type,
            "_code": code,
            "market": market,
            "trade_date": trade_date,
            "ts_utc": ts_utc,
            "score": score,
            "risk_level": risk_level,
            "message": message,
            "payload_json": json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ),
            "_run_id": self.run_id,
            "_fetched_at": fetched_at,
        }

    def _insert_or_replace(
        self,
        table: str,
        columns: tuple[str, ...],
        rows: list[dict[str, Any]],
    ) -> None:
        """Insert or replace records into a table with identical column names."""

        if not rows:
            return
        placeholders = ", ".join("?" for _ in columns)
        sql = f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) VALUES ({placeholders})"
        values = [[row.get(column) for column in columns] for row in rows]
        self.conn.executemany(sql, values)
        self.conn.commit()

    def checkpoint(self) -> None:
        """Merge WAL content into the main SQLite file."""

        with self.lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")


class TickWriter:
    """Background writer that batches realtime rows before SQLite upsert."""

    def __init__(
        self,
        store: TickStore,
        batch_size: int,
        flush_interval: float,
        order_book_slippage_qty: float = 1000.0,
        dark_pool_proxy_config: DarkPoolProxyConfig | None = None,
        l2_imbalance_config: L2ImbalanceConfig | None = None,
    ) -> None:
        self.store = store
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        self.order_book_slippage_qty = order_book_slippage_qty
        self.dark_pool_tracker = DarkPoolProxyTracker(dark_pool_proxy_config)
        self.l2_imbalance_config = l2_imbalance_config or L2ImbalanceConfig()
        self.l2_tracker = L2ImbalanceTracker(self.l2_imbalance_config)
        self.queue: queue.Queue[TickBatch | OrderBookBatch | None] = queue.Queue()
        self.previous_metrics: dict[str, dict[str, Any]] = {}
        self.thread = threading.Thread(target=self._run, name="tick-writer", daemon=True)

    def start(self) -> None:
        """Start the writer thread."""

        self.thread.start()

    def enqueue(self, frame: pd.DataFrame, source: str) -> None:
        """Queue a raw ticker DataFrame for persistence."""

        if frame is not None and not frame.empty:
            self.queue.put(TickBatch(frame=frame.copy(), source=source))

    def enqueue_order_book(self, data: dict[str, Any], source: str) -> None:
        """Queue a full order book snapshot for persistence."""

        if data:
            self.queue.put(OrderBookBatch(data=dict(data), source=source))

    def stop(self) -> None:
        """Stop the writer thread after flushing pending rows."""

        self.queue.put(None)
        self.thread.join(timeout=30)

    def _run(self) -> None:
        pending: list[dict[str, Any]] = []
        last_flush = time.monotonic()
        while True:
            timeout = max(0.1, self.flush_interval)
            try:
                item = self.queue.get(timeout=timeout)
            except queue.Empty:
                item = None
                should_exit = False
            else:
                should_exit = item is None

            if item is not None:
                if isinstance(item, TickBatch):
                    rows = frame_to_records(item.frame, item.source, self.store.run_id)
                    pending.extend(rows)
                    metrics = self.dark_pool_tracker.update(item.frame)
                    self.store.insert_dark_pool_proxy_metrics(metrics, item.source)
                else:
                    self._write_order_book(item)

            elapsed = time.monotonic() - last_flush
            if pending and (len(pending) >= self.batch_size or elapsed >= self.flush_interval):
                self.store.insert_records(pending)
                pending = []
                last_flush = time.monotonic()

            if should_exit:
                if pending:
                    self.store.insert_records(pending)
                return

    def _write_order_book(self, item: OrderBookBatch) -> None:
        code = str(item.data.get("code") or item.data.get("Code") or "")
        previous = self.previous_metrics.get(code)
        snapshot_id = uuid.uuid4().hex
        snapshot, levels, metrics = build_order_book_records(
            item.data,
            run_id=self.store.run_id,
            source=item.source,
            snapshot_id=snapshot_id,
            previous_metrics=previous,
            slippage_qty=self.order_book_slippage_qty,
        )
        self.store.insert_order_book_records(snapshot, levels, metrics)
        signal = self.l2_tracker.update(item.data)
        if signal is not None:
            self.store.insert_l2_imbalance_signal(
                _l2_signal_record(
                    snapshot,
                    signal,
                    self.l2_imbalance_config.level,
                    self.store.run_id,
                    str(snapshot["_fetched_at"]),
                )
            )
        self.previous_metrics[code] = compute_order_book_metrics(
            item.data,
            slippage_qty=self.order_book_slippage_qty,
            previous=previous,
        )


class TickPushHandler(ft.TickerHandlerBase):
    """moomoo push handler that forwards ticker rows to the writer."""

    def __init__(
        self,
        enqueue: Callable[[pd.DataFrame, str], None],
        log_error: Callable[[str | None, str, str], None],
    ) -> None:
        super().__init__()
        self.enqueue = enqueue
        self.log_error = log_error

    def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, pd.DataFrame | str]:
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != ft.RET_OK:
            self.log_error(None, "push_parse", str(data))
            return ft.RET_ERROR, data
        self.enqueue(data, "push")
        return ft.RET_OK, data


class OrderBookPushHandler(ft.OrderBookHandlerBase):
    """moomoo push handler that forwards L2 order book snapshots."""

    def __init__(
        self,
        enqueue: Callable[[dict[str, Any], str], None],
        log_error: Callable[[str | None, str, str], None],
    ) -> None:
        super().__init__()
        self.enqueue = enqueue
        self.log_error = log_error

    def on_recv_rsp(self, rsp_pb: Any) -> tuple[int, dict[str, Any] | str]:
        ret, data = super().on_recv_rsp(rsp_pb)
        if ret != ft.RET_OK:
            self.log_error(None, "order_book_push_parse", str(data))
            return ft.RET_ERROR, data
        if isinstance(data, dict):
            self.enqueue(data, "push")
        return ft.RET_OK, data


def subscribe_codes(
    quote_ctx: ft.OpenQuoteContext,
    store: TickStore,
    codes: tuple[str, ...],
    configs: dict[str, MarketConfig],
    batch_size: int,
    collect_ticks: bool,
    collect_order_book: bool,
    collect_broker_queue: bool,
) -> int:
    """Subscribe selected codes to realtime microstructure pushes."""

    subscribed = 0
    base_sub_types: list[Any] = []
    if collect_ticks:
        base_sub_types.append(ft.SubType.TICKER)
    if collect_order_book:
        base_sub_types.append(ft.SubType.ORDER_BOOK)
    if not base_sub_types and not collect_broker_queue:
        raise ValueError("at least one collection subtype must be enabled")
    for market in ("US", "HK"):
        market_codes = tuple(code for code in codes if market_for_code(code) == market)
        if not market_codes:
            continue
        sub_types = list(base_sub_types)
        if market == "HK" and collect_broker_queue:
            sub_types.append(ft.SubType.BROKER)
        if not sub_types:
            continue
        cfg = configs[market]
        for batch in chunked(market_codes, batch_size):
            ret, msg = quote_ctx.subscribe(
                list(batch),
                sub_types,
                subscribe_push=True,
                session=cfg.session,
            )
            if ret != ft.RET_OK:
                store.log_error(",".join(batch), "subscribe", str(msg))
                continue
            subscribed += len(batch)
            print(f"[{market}] subscribed {len(batch)} symbols {sub_types}", flush=True)
    return subscribed


def fetch_broker_queues(
    quote_ctx: ft.OpenQuoteContext,
    store: TickStore,
    codes: tuple[str, ...],
    source: str,
) -> int:
    """Fetch and persist HK broker queue snapshots for selected codes."""

    total = 0
    for code in codes:
        if market_for_code(code) != "HK":
            continue
        ret, bid_frame, ask_frame = quote_ctx.get_broker_queue(code)
        if ret != ft.RET_OK:
            store.log_error(code, "get_broker_queue", str(bid_frame))
            continue
        if not isinstance(bid_frame, pd.DataFrame):
            bid_frame = pd.DataFrame()
        if not isinstance(ask_frame, pd.DataFrame):
            ask_frame = pd.DataFrame()
        store.insert_broker_queue_records(code, bid_frame, ask_frame, source)
        total += 1
    if total:
        print(f"[HK:broker_queue] snapshots={total} source={source}", flush=True)
    return total


def fetch_quote_snapshots(
    quote_ctx: ft.OpenQuoteContext,
    store: TickStore,
    codes: tuple[str, ...],
    batch_size: int,
    source: str,
) -> int:
    """Fetch and persist low-frequency market snapshots for selected codes."""

    total = 0
    for batch in chunked(codes, batch_size):
        ret, data = quote_ctx.get_market_snapshot(list(batch))
        if ret != ft.RET_OK:
            store.log_error(",".join(batch), "get_market_snapshot", str(data))
            continue
        if not isinstance(data, pd.DataFrame) or data.empty:
            continue
        total += store.insert_quote_snapshot_records(data, source)
    if total:
        print(f"[quote_snapshot] rows={total} source={source}", flush=True)
    return total


def backfill_recent_cache(
    quote_ctx: ft.OpenQuoteContext,
    writer: TickWriter,
    store: TickStore,
    codes: tuple[str, ...],
    num: int,
) -> None:
    """Fetch the latest cached ticker rows after subscription."""

    for code in codes:
        ret, data = quote_ctx.get_rt_ticker(code, num)
        if ret != ft.RET_OK:
            store.log_error(code, "get_rt_ticker", str(data))
            continue
        if isinstance(data, pd.DataFrame) and not data.empty:
            writer.enqueue(data, "cache")
            print(f"[{code}] cached_ticks rows={len(data)}", flush=True)
        else:
            print(f"[{code}] cached_ticks rows=0", flush=True)


def fetch_initial_order_books(
    quote_ctx: ft.OpenQuoteContext,
    writer: TickWriter,
    store: TickStore,
    codes: tuple[str, ...],
    levels: int,
) -> None:
    """Fetch the latest full L2 order book after subscription."""

    for code in codes:
        ret, data = quote_ctx.get_order_book(code, num=levels)
        if ret != ft.RET_OK:
            store.log_error(code, "get_order_book", str(data))
            continue
        if isinstance(data, dict) and (data.get("Bid") or data.get("Ask")):
            writer.enqueue_order_book(data, "cache")
            bid_count = len(data.get("Bid") or [])
            ask_count = len(data.get("Ask") or [])
            print(
                f"[{code}] cached_order_book bid={bid_count} ask={ask_count}",
                flush=True,
            )
        else:
            print(f"[{code}] cached_order_book bid=0 ask=0", flush=True)


def wait_until_done(
    duration_seconds: int,
    stop_event: threading.Event,
    poll_callbacks: Iterable[tuple[Callable[[], None], float]] | None = None,
) -> None:
    """Block until duration elapses, Ctrl-C is received, or task stop is requested."""

    polls = [
        PeriodicPoll(
            callback=callback,
            interval=max(1.0, float(interval)),
            next_due=time.monotonic() + max(1.0, float(interval)),
        )
        for callback, interval in (poll_callbacks or ())
    ]

    def run_due_polls() -> None:
        now = time.monotonic()
        for poll in polls:
            if now >= poll.next_due:
                poll.callback()
                poll.next_due = now + poll.interval

    if duration_seconds <= 0:
        while not stop_event.is_set():
            run_due_polls()
            time.sleep(1)
        return

    deadline = time.monotonic() + duration_seconds
    while not stop_event.is_set() and time.monotonic() < deadline:
        run_due_polls()
        time.sleep(1)


def main() -> int:
    """Collect realtime moomoo ticker rows into a local SQLite database."""

    args = parse_args()
    if (
        not args.collect_ticks
        and not args.collect_order_book
        and not args.collect_broker_queue
        and not args.collect_quote_snapshots
    ):
        raise ValueError("at least one realtime collection type is required")
    if args.init_db_only:
        store = TickStore(Path(args.db), uuid.uuid4().hex)
        store.close()
        print(f"initialized tick tables in {args.db}", flush=True)
        return 0

    codes = resolve_codes(args)
    markets = tuple(sorted({market_for_code(code) for code in codes}))
    configs = market_configs(args)
    collect_broker_queue = bool(args.collect_broker_queue and "HK" in markets)
    collect_quote_snapshots = bool(args.collect_quote_snapshots)
    needs_subscription = (
        args.collect_ticks or args.collect_order_book or collect_broker_queue
    )
    run_id = uuid.uuid4().hex
    store = TickStore(Path(args.db), run_id)
    writer = TickWriter(
        store,
        args.batch_size,
        args.flush_interval,
        args.order_book_slippage_qty,
        dark_pool_proxy_config=DarkPoolProxyConfig(
            us_min_notional=args.dark_pool_us_min_notional,
            hk_min_notional=args.dark_pool_hk_min_notional,
        ),
        l2_imbalance_config=L2ImbalanceConfig(
            level=args.l2_imbalance_level,
            warn=args.l2_imbalance_warn,
            danger=args.l2_imbalance_danger,
        ),
    )
    stop_event = threading.Event()

    def handle_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, handle_stop)
    signal.signal(signal.SIGTERM, handle_stop)

    status = "success"
    note = None
    store.start_run(markets, codes)
    writer.start()
    print(
        f"run_id={run_id} db={args.db} markets={markets} codes={len(codes)}",
        flush=True,
    )
    quote_ctx = ft.OpenQuoteContext(host=args.host, port=args.port)
    try:
        if args.collect_ticks:
            quote_ctx.set_handler(TickPushHandler(writer.enqueue, store.log_error))
        if args.collect_order_book:
            quote_ctx.set_handler(
                OrderBookPushHandler(writer.enqueue_order_book, store.log_error)
            )
        quote_ctx.start()
        if needs_subscription:
            subscribed = subscribe_codes(
                quote_ctx,
                store,
                codes,
                configs,
                args.subscribe_batch_size,
                args.collect_ticks,
                args.collect_order_book,
                collect_broker_queue,
            )
            if subscribed == 0:
                status = "failed"
                note = "no realtime subscriptions succeeded"
                return 2
        if args.post_subscribe_wait > 0:
            time.sleep(args.post_subscribe_wait)
        if args.collect_ticks:
            backfill_recent_cache(quote_ctx, writer, store, codes, args.cache_num)
        if args.collect_order_book:
            fetch_initial_order_books(
                quote_ctx,
                writer,
                store,
                codes,
                args.order_book_levels,
            )
        if collect_broker_queue:
            fetch_broker_queues(quote_ctx, store, codes, "cache")
        if collect_quote_snapshots:
            fetch_quote_snapshots(
                quote_ctx,
                store,
                codes,
                args.quote_snapshot_batch_size,
                "cache",
            )
        poll_callbacks: list[tuple[Callable[[], None], float]] = []
        if collect_broker_queue:
            poll_callbacks.append(
                (
                    lambda: fetch_broker_queues(quote_ctx, store, codes, "poll"),
                    args.broker_queue_interval,
                )
            )
        if collect_quote_snapshots:
            poll_callbacks.append(
                (
                    lambda: fetch_quote_snapshots(
                        quote_ctx,
                        store,
                        codes,
                        args.quote_snapshot_batch_size,
                        "poll",
                    ),
                    args.quote_snapshot_interval,
                )
            )
        wait_until_done(
            args.duration_seconds,
            stop_event,
            poll_callbacks,
        )
    except Exception as exc:
        status = "failed"
        note = f"{type(exc).__name__}: {exc}"
        store.log_error(None, "main", note)
        raise
    finally:
        try:
            quote_ctx.unsubscribe_all()
        except Exception as exc:
            store.log_error(None, "unsubscribe_all", str(exc))
        quote_ctx.close()
        writer.stop()
        store.rebuild_microstructure_daily_features()
        store.finish_run(status, note)
        store.checkpoint()
        store.close()
    print(
        "finished "
        f"run_id={run_id} status={status} "
        f"tick_rows={store.rows_written} "
        f"book_snapshots={store.order_book_snapshots} "
        f"quote_snapshots={store.quote_snapshots}",
        flush=True,
    )
    return 0 if status == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
