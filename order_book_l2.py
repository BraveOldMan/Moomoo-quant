from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


METRIC_LEVELS: tuple[int, ...] = (1, 5, 10, 50)


@dataclass(frozen=True)
class OrderBookLevel:
    """One normalized L2 price level from a moomoo order book snapshot."""

    price: float
    volume: float
    order_count: int | None
    detail: dict[str, Any]

    def as_moomoo_tuple(self) -> tuple[float, float, int, dict[str, Any]]:
        """Return the tuple shape used by moomoo get_order_book."""

        return (
            self.price,
            self.volume,
            int(self.order_count or 0),
            dict(self.detail),
        )


@dataclass(frozen=True)
class CachedOrderBook:
    """Latest normalized order book for one symbol."""

    data: dict[str, Any]
    updated_monotonic: float
    updated_utc: str


@dataclass(frozen=True)
class L2ImbalanceConfig:
    """Configuration for visible-book imbalance tracking."""

    level: int = 10
    warn: float = 0.35
    danger: float = 0.60
    persist_snapshots: int = 3
    alert_cooldown_s: float = 300.0
    spread_warning_bps: float = 5.0
    spread_danger_bps: float = 30.0
    slippage_warning_bps: float = 10.0
    slippage_danger_bps: float = 50.0


@dataclass(frozen=True)
class L2ImbalanceSignal:
    """Current L2 imbalance state for one symbol."""

    code: str
    score: float
    imbalance: float | None
    direction: str
    risk_level: str
    reasons: tuple[str, ...]
    metrics: dict[str, float | None]
    consecutive_high_risk: int = 0
    should_alert: bool = False

    def alert_message(self) -> str:
        """Return a concise alert message."""

        imbalance_text = (
            "NA" if self.imbalance is None else f"{self.imbalance:+.3f}"
        )
        reason_text = "；".join(self.reasons) if self.reasons else "无"
        return (
            f"{self.code} score={self.score:.1f} "
            f"imbalance={imbalance_text} "
            f"level={self.risk_level} "
            f"streak={self.consecutive_high_risk} "
            f"{reason_text}"
        )


class OrderBookCache:
    """Thread-safe latest L2 order book cache for realtime strategy use.

    The cache stores moomoo-style full snapshots. It does not try to rebuild
    L3 order events or infer queue position.
    """

    def __init__(self, max_age_s: float = 3.0) -> None:
        self.max_age_s = max(0.1, float(max_age_s))
        self._books: dict[str, CachedOrderBook] = {}
        self._lock = threading.Lock()

    def update(self, data: dict[str, Any]) -> None:
        """Store the latest snapshot for its code."""

        code = str(data.get("code") or data.get("Code") or "")
        if not code:
            return
        with self._lock:
            self._books[code] = CachedOrderBook(
                data=copy_order_book(data),
                updated_monotonic=_monotonic_now(),
                updated_utc=utc_now(),
            )

    def get(self, code: str, min_levels: int = 1) -> dict[str, Any] | None:
        """Return a fresh moomoo-style snapshot when enough levels exist."""

        with self._lock:
            cached = self._books.get(code)
            if cached is None:
                return None
            if _monotonic_now() - cached.updated_monotonic > self.max_age_s:
                return None
            data = copy_order_book(cached.data)
        if len(data.get("Bid") or []) < min_levels:
            return None
        if len(data.get("Ask") or []) < min_levels:
            return None
        return data


class L2ImbalanceTracker:
    """Stateful realtime tracker for visible L2 order book imbalance.

    The tracker uses full moomoo L2 snapshots. It flags persistent ask-heavy
    pressure and deteriorating visible liquidity, but does not infer NYSE NOI
    auction imbalance or L3 order-level queue events.
    """

    def __init__(self, config: L2ImbalanceConfig | None = None) -> None:
        self.config = config or L2ImbalanceConfig()
        self._previous_metrics: dict[str, dict[str, float | None]] = {}
        self._high_risk_counts: dict[str, int] = {}
        self._last_alert_at: dict[str, float] = {}
        self._latest: dict[str, L2ImbalanceSignal] = {}
        self._lock = threading.Lock()

    def update(self, data: dict[str, Any]) -> L2ImbalanceSignal | None:
        """Update tracker state with one order book snapshot."""

        code = str(data.get("code") or data.get("Code") or "")
        if not code:
            return None
        levels = metric_levels_for(self.config.level)
        with self._lock:
            previous = self._previous_metrics.get(code)
            metrics = compute_order_book_metrics(
                data,
                levels=levels,
                previous=previous,
            )
            base_signal = evaluate_l2_imbalance(
                metrics,
                config=self.config,
                code=code,
            )
            high_risk = base_signal.risk_level == "danger"
            count = self._high_risk_counts.get(code, 0) + 1 if high_risk else 0
            self._high_risk_counts[code] = count
            now = _monotonic_now()
            last_alert = self._last_alert_at.get(code, -1_000_000_000.0)
            should_alert = (
                high_risk
                and count >= max(1, self.config.persist_snapshots)
                and now - last_alert >= self.config.alert_cooldown_s
            )
            if should_alert:
                self._last_alert_at[code] = now
            signal = evaluate_l2_imbalance(
                metrics,
                config=self.config,
                code=code,
                consecutive_high_risk=count,
                should_alert=should_alert,
            )
            self._previous_metrics[code] = metrics
            self._latest[code] = signal
            return signal

    def latest(self, code: str) -> L2ImbalanceSignal | None:
        """Return the latest signal for a symbol."""

        with self._lock:
            return self._latest.get(code)


def utc_now() -> str:
    """Return current UTC timestamp with microsecond precision."""

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _monotonic_now() -> float:
    import time

    return time.monotonic()


def market_for_code(code: str) -> str:
    """Return the market label from a moomoo symbol."""

    if code.startswith("US."):
        return "US"
    if code.startswith("HK."):
        return "HK"
    return ""


def copy_order_book(data: dict[str, Any]) -> dict[str, Any]:
    """Copy an order book while normalizing Bid and Ask levels."""

    copied = dict(data)
    copied["Bid"] = [level.as_moomoo_tuple() for level in normalize_levels(data.get("Bid"))]
    copied["Ask"] = [level.as_moomoo_tuple() for level in normalize_levels(data.get("Ask"))]
    return copied


def normalize_levels(raw_levels: Any) -> list[OrderBookLevel]:
    """Normalize moomoo order book levels into typed records."""

    if not raw_levels:
        return []
    levels: list[OrderBookLevel] = []
    for raw in raw_levels:
        try:
            price = float(raw[0])
            volume = float(raw[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not math.isfinite(price) or not math.isfinite(volume):
            continue
        order_count = None
        try:
            order_count = int(raw[2])
        except (TypeError, ValueError, IndexError):
            order_count = None
        detail: dict[str, Any] = {}
        try:
            if isinstance(raw[3], dict):
                detail = dict(raw[3])
        except IndexError:
            detail = {}
        levels.append(
            OrderBookLevel(
                price=price,
                volume=max(0.0, volume),
                order_count=order_count,
                detail=detail,
            )
        )
    return levels


def sum_depth(levels: Any, count: int) -> float:
    """Return cumulative volume over the first count levels."""

    return sum(level.volume for level in normalize_levels(levels)[: max(0, count)])


def compute_order_book_metrics(
    data: dict[str, Any],
    *,
    levels: tuple[int, ...] = METRIC_LEVELS,
    slippage_qty: float = 1000.0,
    previous: dict[str, Any] | None = None,
) -> dict[str, float | None]:
    """Compute L2 metrics from a moomoo order book snapshot.

    The input is a full L2 snapshot, not an event stream. Slippage is estimated
    by walking the current visible book for the requested share quantity.
    """

    bids = normalize_levels(data.get("Bid"))
    asks = normalize_levels(data.get("Ask"))
    metrics: dict[str, float | None] = {
        "best_bid": bids[0].price if bids else None,
        "best_ask": asks[0].price if asks else None,
        "mid_price": None,
        "spread": None,
        "spread_bps": None,
        "micro_price": None,
        "estimated_buy_slippage_bps": None,
        "estimated_sell_slippage_bps": None,
        "depth_change_rate": None,
    }
    if bids and asks:
        best_bid = bids[0].price
        best_ask = asks[0].price
        mid = (best_bid + best_ask) / 2.0
        spread = best_ask - best_bid
        bid_vol = bids[0].volume
        ask_vol = asks[0].volume
        denom = bid_vol + ask_vol
        metrics.update(
            {
                "mid_price": mid,
                "spread": spread,
                "spread_bps": spread / mid * 10_000.0 if mid > 0 else None,
                "micro_price": (
                    (best_ask * bid_vol + best_bid * ask_vol) / denom
                    if denom > 0
                    else mid
                ),
                "estimated_buy_slippage_bps": estimate_slippage_bps(
                    asks, slippage_qty, mid
                ),
                "estimated_sell_slippage_bps": estimate_slippage_bps(
                    bids, slippage_qty, mid
                ),
            }
        )

    for level in levels:
        bid_depth = sum(level_record.volume for level_record in bids[:level])
        ask_depth = sum(level_record.volume for level_record in asks[:level])
        total = bid_depth + ask_depth
        metrics[f"bid_depth_{level}"] = bid_depth
        metrics[f"ask_depth_{level}"] = ask_depth
        metrics[f"imbalance_{level}"] = (
            (bid_depth - ask_depth) / total if total > 0 else None
        )

    if previous is not None:
        current_depth = _metric_float(metrics.get("bid_depth_10")) + _metric_float(
            metrics.get("ask_depth_10")
        )
        previous_depth = _metric_float(previous.get("bid_depth_10")) + _metric_float(
            previous.get("ask_depth_10")
        )
        if previous_depth > 0:
            metrics["depth_change_rate"] = (current_depth - previous_depth) / previous_depth
    return metrics


def metric_levels_for(primary_level: int) -> tuple[int, ...]:
    """Return standard metric levels plus the configured primary level."""

    levels = set(METRIC_LEVELS)
    levels.add(max(1, int(primary_level)))
    return tuple(sorted(levels))


def evaluate_l2_imbalance(
    metrics: dict[str, float | None],
    *,
    config: L2ImbalanceConfig | None = None,
    code: str = "",
    consecutive_high_risk: int = 0,
    should_alert: bool = False,
) -> L2ImbalanceSignal:
    """Evaluate visible L2 imbalance as a 0-100 risk score."""

    cfg = config or L2ImbalanceConfig()
    level = max(1, int(cfg.level))
    imbalance = _optional_metric_float(metrics.get(f"imbalance_{level}"))
    reasons: list[str] = []
    if imbalance is None:
        base_score = 50.0
        direction = "unavailable"
        reasons.append("盘口深度不足")
    else:
        base_score = _clamp(50.0 - imbalance * 50.0)
        if imbalance <= -cfg.danger:
            direction = "ask_heavy"
            reasons.append("卖盘深度显著占优")
        elif imbalance <= -cfg.warn:
            direction = "ask_heavy"
            reasons.append("卖盘深度占优")
        elif imbalance >= cfg.danger:
            direction = "bid_heavy"
            reasons.append("买盘深度显著占优")
        elif imbalance >= cfg.warn:
            direction = "bid_heavy"
            reasons.append("买盘深度占优")
        else:
            direction = "balanced"

    spread_bps = _optional_metric_float(metrics.get("spread_bps"))
    spread_penalty = _threshold_penalty(
        spread_bps,
        cfg.spread_warning_bps,
        cfg.spread_danger_bps,
        20.0,
    )
    if spread_bps is not None and spread_bps >= cfg.spread_warning_bps:
        reasons.append(f"价差扩大 {spread_bps:.1f}bps")

    buy_slippage = _optional_metric_float(metrics.get("estimated_buy_slippage_bps"))
    sell_slippage = _optional_metric_float(metrics.get("estimated_sell_slippage_bps"))
    slippage_bps = max(
        [value for value in (buy_slippage, sell_slippage) if value is not None],
        default=None,
    )
    slippage_penalty = _threshold_penalty(
        slippage_bps,
        cfg.slippage_warning_bps,
        cfg.slippage_danger_bps,
        20.0,
    )
    if slippage_bps is not None and slippage_bps >= cfg.slippage_warning_bps:
        reasons.append(f"可见盘口滑点扩大 {slippage_bps:.1f}bps")

    depth_change = _optional_metric_float(metrics.get("depth_change_rate"))
    depth_penalty = 0.0
    if depth_change is not None and depth_change < 0:
        depth_penalty = min(20.0, abs(depth_change) * 50.0)
        if depth_change <= -0.2:
            reasons.append(f"10档深度下降 {depth_change:.1%}")

    score = _clamp(base_score + spread_penalty + slippage_penalty + depth_penalty)
    risk_level = _risk_level(score, imbalance, cfg)
    return L2ImbalanceSignal(
        code=code,
        score=score,
        imbalance=imbalance,
        direction=direction,
        risk_level=risk_level,
        reasons=tuple(reasons),
        metrics=dict(metrics),
        consecutive_high_risk=consecutive_high_risk,
        should_alert=should_alert,
    )


def estimate_slippage_bps(
    levels: list[OrderBookLevel],
    target_qty: float,
    reference_price: float | None,
) -> float | None:
    """Estimate visible-book slippage in basis points for target quantity."""

    if not levels or target_qty <= 0 or not reference_price or reference_price <= 0:
        return None
    remaining = float(target_qty)
    notional = 0.0
    filled = 0.0
    for level in levels:
        take = min(remaining, level.volume)
        if take <= 0:
            continue
        notional += take * level.price
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    if filled <= 0:
        return None
    average_price = notional / filled
    return abs(average_price - reference_price) / reference_price * 10_000.0


def build_order_book_records(
    data: dict[str, Any],
    *,
    run_id: str,
    source: str,
    snapshot_id: str,
    previous_metrics: dict[str, Any] | None = None,
    slippage_qty: float = 1000.0,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Build normalized SQLite rows for one order book snapshot."""

    copied = copy_order_book(data)
    code = str(copied.get("code") or copied.get("Code") or "")
    market = market_for_code(code)
    fetched_at = utc_now()
    bid_levels = normalize_levels(copied.get("Bid"))
    ask_levels = normalize_levels(copied.get("Ask"))
    metrics = compute_order_book_metrics(
        copied,
        slippage_qty=slippage_qty,
        previous=previous_metrics,
    )
    snapshot_ts = _snapshot_time(copied, fetched_at)
    payload = json.dumps(copied, ensure_ascii=False, sort_keys=True, default=str)
    snapshot = {
        "snapshot_id": snapshot_id,
        "_code": code,
        "market": market,
        "name": copied.get("name"),
        "snapshot_ts_utc": snapshot_ts,
        "source": source,
        "bid_svr_recv_time": copied.get("svr_recv_time_bid"),
        "ask_svr_recv_time": copied.get("svr_recv_time_ask"),
        "bid_svr_recv_time_timestamp": copied.get("svr_recv_time_bid_timestamp"),
        "ask_svr_recv_time_timestamp": copied.get("svr_recv_time_ask_timestamp"),
        "order_book_type": copied.get("order_book_type"),
        "bid_level_count": len(bid_levels),
        "ask_level_count": len(ask_levels),
        "bid_levels_json": json.dumps(
            [level.as_moomoo_tuple() for level in bid_levels],
            ensure_ascii=False,
            default=str,
        ),
        "ask_levels_json": json.dumps(
            [level.as_moomoo_tuple() for level in ask_levels],
            ensure_ascii=False,
            default=str,
        ),
        "_run_id": run_id,
        "_fetched_at": fetched_at,
        "_payload_json": payload,
    }
    level_rows = _level_rows(
        snapshot_id=snapshot_id,
        code=code,
        market=market,
        snapshot_ts=snapshot_ts,
        source=source,
        run_id=run_id,
        bids=bid_levels,
        asks=ask_levels,
    )
    metric_record = {
        "snapshot_id": snapshot_id,
        "_code": code,
        "market": market,
        "snapshot_ts_utc": snapshot_ts,
        "_run_id": run_id,
        "_fetched_at": fetched_at,
        "metrics_json": json.dumps(metrics, ensure_ascii=False, sort_keys=True),
        **metrics,
    }
    return snapshot, level_rows, metric_record


def _level_rows(
    *,
    snapshot_id: str,
    code: str,
    market: str,
    snapshot_ts: str,
    source: str,
    run_id: str,
    bids: list[OrderBookLevel],
    asks: list[OrderBookLevel],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for side, levels in (("BID", bids), ("ASK", asks)):
        for index, level in enumerate(levels, start=1):
            rows.append(
                {
                    "snapshot_id": snapshot_id,
                    "_code": code,
                    "market": market,
                    "snapshot_ts_utc": snapshot_ts,
                    "side": side,
                    "level": index,
                    "price": level.price,
                    "volume": level.volume,
                    "order_count": level.order_count,
                    "detail_json": json.dumps(
                        level.detail,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    ),
                    "source": source,
                    "_run_id": run_id,
                }
            )
    return rows


def _snapshot_time(data: dict[str, Any], fallback: str) -> str:
    for key in ("svr_recv_time_bid_timestamp", "svr_recv_time_ask_timestamp"):
        raw = data.get(key)
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat(
                timespec="microseconds"
            )
    return fallback


def _metric_float(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _optional_metric_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _threshold_penalty(
    value: float | None,
    warning: float,
    danger: float,
    max_penalty: float,
) -> float:
    if value is None or value < warning:
        return 0.0
    if danger <= warning:
        return max_penalty
    return min(max_penalty, (value - warning) / (danger - warning) * max_penalty)


def _risk_level(
    score: float,
    imbalance: float | None,
    config: L2ImbalanceConfig,
) -> str:
    if score >= 70.0 or (imbalance is not None and imbalance <= -config.danger):
        return "danger"
    if score >= 60.0 or (imbalance is not None and imbalance <= -config.warn):
        return "warning"
    if score <= 35.0 or (imbalance is not None and imbalance >= config.warn):
        return "support"
    return "neutral"


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if not math.isfinite(x):
        return (lo + hi) / 2.0
    return max(lo, min(hi, x))
