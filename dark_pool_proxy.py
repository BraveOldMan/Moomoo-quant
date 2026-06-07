from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True)
class DarkPoolProxyConfig:
    """Config for moomoo large-print proxy scanning.

    This is not a TRF dark-pool classifier. It only scans visible moomoo
    ticker rows for unusually large prints.
    """

    us_min_notional: float = 100_000.0
    hk_min_notional: float = 800_000.0
    alert_cooldown_s: float = 300.0
    max_seen_sequences: int = 10_000


@dataclass(frozen=True)
class DarkPoolProxyPrint:
    """One large visible ticker print from moomoo."""

    code: str
    market: str
    sequence: str
    time: str
    price: float
    volume: float
    notional: float
    direction: str

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        return {
            "code": self.code,
            "market": self.market,
            "sequence": self.sequence,
            "time": self.time,
            "price": self.price,
            "volume": self.volume,
            "notional": self.notional,
            "direction": self.direction,
        }


@dataclass(frozen=True)
class DarkPoolProxyMetrics:
    """Aggregated large-print proxy metrics for one symbol."""

    code: str
    market: str
    threshold: float
    print_count: int
    buy_count: int
    sell_count: int
    unknown_count: int
    total_notional: float
    buy_notional: float
    sell_notional: float
    unknown_notional: float
    largest_notional: float
    net_ratio: float | None
    score: float
    risk_level: str
    latest_time: str | None
    prints: tuple[DarkPoolProxyPrint, ...]
    should_alert: bool = False

    def as_dict(self, *, include_prints: bool = True) -> dict[str, Any]:
        """Return a JSON-serializable representation."""

        out: dict[str, Any] = {
            "code": self.code,
            "market": self.market,
            "threshold": self.threshold,
            "print_count": self.print_count,
            "buy_count": self.buy_count,
            "sell_count": self.sell_count,
            "unknown_count": self.unknown_count,
            "total_notional": self.total_notional,
            "buy_notional": self.buy_notional,
            "sell_notional": self.sell_notional,
            "unknown_notional": self.unknown_notional,
            "largest_notional": self.largest_notional,
            "net_ratio": self.net_ratio,
            "score": self.score,
            "risk_level": self.risk_level,
            "latest_time": self.latest_time,
            "should_alert": self.should_alert,
        }
        if include_prints:
            out["prints"] = [item.as_dict() for item in self.prints]
        return out

    def alert_message(self) -> str:
        """Return a concise alert message for the latest large print."""

        side = "SELL" if self.sell_notional > self.buy_notional else "BUY"
        latest = self.prints[-1] if self.prints else None
        latest_text = ""
        if latest is not None:
            latest_text = (
                f" latest={latest.direction} "
                f"{latest.volume:.0f}@{latest.price:.3f} "
                f"notional={latest.notional:,.0f}"
            )
        return (
            f"{self.code} {side} large-print proxy "
            f"score={self.score:.1f} count={self.print_count} "
            f"total={self.total_notional:,.0f} "
            f"net_ratio={self.net_ratio if self.net_ratio is not None else 'NA'}"
            f"{latest_text}"
        )


class DarkPoolProxyTracker:
    """Stateful large-print proxy tracker with sequence dedupe and cooldown."""

    def __init__(self, config: DarkPoolProxyConfig | None = None) -> None:
        self.config = config or DarkPoolProxyConfig()
        self._seen: dict[str, set[str]] = {}
        self._last_alert_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def update(
        self,
        frame: Any,
        *,
        market_date: str | None = None,
    ) -> list[DarkPoolProxyMetrics]:
        """Update tracker from a moomoo ticker frame and return fresh metrics."""

        with self._lock:
            metrics_by_code = scan_dark_pool_proxy(
                frame,
                config=self.config,
                market_date=market_date,
                seen_sequences=self._seen,
            )
            now = time.monotonic()
            out: list[DarkPoolProxyMetrics] = []
            for code, metrics in metrics_by_code.items():
                seen = self._seen.setdefault(code, set())
                if len(seen) > self.config.max_seen_sequences:
                    seen.clear()
                last = self._last_alert_at.get(code, -1_000_000_000.0)
                should_alert = (
                    metrics.print_count > 0
                    and now - last >= self.config.alert_cooldown_s
                )
                if should_alert:
                    self._last_alert_at[code] = now
                out.append(replace(metrics, should_alert=should_alert))
            return out


def scan_dark_pool_proxy(
    frame: Any,
    *,
    config: DarkPoolProxyConfig | None = None,
    market_date: str | None = None,
    code: str | None = None,
    seen_sequences: dict[str, set[str]] | None = None,
) -> dict[str, DarkPoolProxyMetrics]:
    """Scan moomoo ticker rows for large-print proxy events.

    The scanner requires visible moomoo ticker rows. It cannot identify FINRA
    TRF dark-pool prints because moomoo rows do not expose exchange=4/trf_id.
    """

    cfg = config or DarkPoolProxyConfig()
    rows = _iter_rows(frame)
    grouped: dict[str, list[DarkPoolProxyPrint]] = {}
    local_seen: set[str] = set()
    for index, row in rows:
        row_code = str(row.get("code") or row.get("_code") or code or "")
        if not row_code:
            continue
        if code is not None and row_code != code:
            continue
        row_date = _row_date(row)
        if market_date is not None and row_date != market_date:
            continue
        price = _safe_float(row.get("price"))
        volume = _safe_float(row.get("volume"))
        if price <= 0 or volume <= 0:
            continue
        notional = _notional(row, price, volume)
        threshold = threshold_for_code(row_code, cfg)
        if notional < threshold:
            continue
        sequence = _sequence_key(row, row_code, index)
        seen = seen_sequences.setdefault(row_code, set()) if seen_sequences is not None else None
        if sequence in local_seen or (seen is not None and sequence in seen):
            continue
        local_seen.add(sequence)
        if seen is not None:
            seen.add(sequence)
        item = DarkPoolProxyPrint(
            code=row_code,
            market=market_for_code(row_code),
            sequence=sequence,
            time=str(row.get("time") or row.get("time_key") or ""),
            price=price,
            volume=volume,
            notional=notional,
            direction=_direction(row.get("ticker_direction")),
        )
        grouped.setdefault(row_code, []).append(item)
    return {item_code: _build_metrics(item_code, cfg, prints) for item_code, prints in grouped.items()}


def market_for_code(code: str) -> str:
    """Return market prefix for a moomoo code."""

    if code.startswith("US."):
        return "US"
    if code.startswith("HK."):
        return "HK"
    return ""


def threshold_for_code(code: str, config: DarkPoolProxyConfig) -> float:
    """Return configured large-print notional threshold for a code."""

    market = market_for_code(code)
    if market == "HK":
        return config.hk_min_notional
    return config.us_min_notional


def dark_pool_proxy_score(buy_notional: float, sell_notional: float) -> float:
    """Return 0-100 risk score from large-print buy/sell notional."""

    total = buy_notional + sell_notional
    if total <= 0:
        return 50.0
    net_ratio = (buy_notional - sell_notional) / total
    return _clamp(50.0 - net_ratio * 50.0)


def _build_metrics(
    code: str,
    config: DarkPoolProxyConfig,
    prints: list[DarkPoolProxyPrint],
) -> DarkPoolProxyMetrics:
    buy = sum(item.notional for item in prints if item.direction == "BUY")
    sell = sum(item.notional for item in prints if item.direction == "SELL")
    unknown = sum(item.notional for item in prints if item.direction not in {"BUY", "SELL"})
    total = buy + sell + unknown
    directional_total = buy + sell
    net_ratio = (buy - sell) / directional_total if directional_total > 0 else None
    score = dark_pool_proxy_score(buy, sell)
    return DarkPoolProxyMetrics(
        code=code,
        market=market_for_code(code),
        threshold=threshold_for_code(code, config),
        print_count=len(prints),
        buy_count=sum(1 for item in prints if item.direction == "BUY"),
        sell_count=sum(1 for item in prints if item.direction == "SELL"),
        unknown_count=sum(1 for item in prints if item.direction not in {"BUY", "SELL"}),
        total_notional=total,
        buy_notional=buy,
        sell_notional=sell,
        unknown_notional=unknown,
        largest_notional=max((item.notional for item in prints), default=0.0),
        net_ratio=net_ratio,
        score=score,
        risk_level=_risk_level(score),
        latest_time=max((item.time for item in prints), default=None),
        prints=tuple(sorted(prints, key=lambda item: item.time)),
    )


def _iter_rows(frame: Any) -> list[tuple[Any, dict[str, Any]]]:
    if frame is None:
        return []
    if hasattr(frame, "empty") and frame.empty:
        return []
    if hasattr(frame, "iterrows"):
        return [(index, row.to_dict()) for index, row in frame.iterrows()]
    if isinstance(frame, list):
        return list(enumerate(item for item in frame if isinstance(item, dict)))
    if isinstance(frame, dict):
        return [(0, frame)]
    return []


def _row_date(row: dict[str, Any]) -> str | None:
    value = str(row.get("time") or row.get("time_key") or row.get("ts_utc") or "")
    return value[:10] if len(value) >= 10 else None


def _notional(row: dict[str, Any], price: float, volume: float) -> float:
    turnover = _safe_float(row.get("turnover"))
    return turnover if turnover > 0 else price * volume


def _direction(value: Any) -> str:
    raw = str(value or "").upper()
    if "BUY" in raw:
        return "BUY"
    if "SELL" in raw:
        return "SELL"
    return "UNKNOWN"


def _sequence_key(row: dict[str, Any], code: str, index: Any) -> str:
    raw = row.get("sequence")
    if raw is not None and str(raw).strip():
        return str(raw)
    return "|".join(
        (
            "fallback",
            code,
            str(row.get("time") or row.get("time_key") or ""),
            str(row.get("price") or ""),
            str(row.get("volume") or ""),
            str(row.get("ticker_direction") or ""),
            str(index),
        )
    )


def _safe_float(value: Any) -> float:
    try:
        result = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _risk_level(score: float) -> str:
    if score >= 70.0:
        return "danger"
    if score >= 60.0:
        return "warning"
    if score <= 35.0:
        return "support"
    return "neutral"


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if not math.isfinite(value):
        return (lo + hi) / 2.0
    return max(lo, min(hi, value))
