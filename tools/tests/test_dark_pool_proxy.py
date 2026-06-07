from __future__ import annotations

import pandas as pd
import pytest

from dark_pool_proxy import (
    DarkPoolProxyConfig,
    DarkPoolProxyTracker,
    dark_pool_proxy_score,
    scan_dark_pool_proxy,
)


def test_scan_us_large_print_proxy_and_dedupes_sequence() -> None:
    frame = pd.DataFrame(
        [
            _row("US.AAPL", 1, "2026-06-05 10:00:00", 200.0, 600, "SELL"),
            _row("US.AAPL", 1, "2026-06-05 10:00:00", 200.0, 600, "SELL"),
            _row("US.AAPL", 2, "2026-06-05 10:01:00", 200.0, 100, "BUY"),
        ]
    )

    metrics = scan_dark_pool_proxy(
        frame,
        config=DarkPoolProxyConfig(us_min_notional=100_000.0),
        market_date="2026-06-05",
        code="US.AAPL",
    )["US.AAPL"]

    assert metrics.print_count == 1
    assert metrics.sell_count == 1
    assert metrics.total_notional == pytest.approx(120_000.0)
    assert metrics.score == pytest.approx(100.0)
    assert metrics.risk_level == "danger"


def test_scan_hk_uses_hk_threshold_and_skips_stale_rows() -> None:
    frame = pd.DataFrame(
        [
            _row("HK.00700", 1, "2026-06-04 10:00:00", 400.0, 3000, "SELL"),
            _row("HK.00700", 2, "2026-06-05 10:01:00", 400.0, 1000, "SELL"),
            _row("HK.00700", 3, "2026-06-05 10:02:00", 400.0, 3000, "BUY"),
        ]
    )

    metrics = scan_dark_pool_proxy(
        frame,
        config=DarkPoolProxyConfig(hk_min_notional=800_000.0),
        market_date="2026-06-05",
        code="HK.00700",
    )["HK.00700"]

    assert metrics.print_count == 1
    assert metrics.buy_count == 1
    assert metrics.total_notional == pytest.approx(1_200_000.0)
    assert metrics.score == pytest.approx(0.0)
    assert metrics.risk_level == "support"


def test_scan_returns_no_metrics_without_large_prints() -> None:
    frame = pd.DataFrame(
        [_row("US.AAPL", 1, "2026-06-05 10:00:00", 100.0, 100, "BUY")]
    )

    assert (
        scan_dark_pool_proxy(
            frame,
            config=DarkPoolProxyConfig(us_min_notional=100_000.0),
            market_date="2026-06-05",
            code="US.AAPL",
        )
        == {}
    )


def test_dark_pool_proxy_tracker_dedupes_and_applies_cooldown() -> None:
    tracker = DarkPoolProxyTracker(
        DarkPoolProxyConfig(us_min_notional=100_000.0, alert_cooldown_s=999.0)
    )
    frame = pd.DataFrame(
        [_row("US.AAPL", 1, "2026-06-05 10:00:00", 200.0, 600, "SELL")]
    )

    first = tracker.update(frame, market_date="2026-06-05")
    second = tracker.update(frame, market_date="2026-06-05")

    assert len(first) == 1
    assert first[0].should_alert
    assert second == []


def test_dark_pool_proxy_score_directional_extremes() -> None:
    assert dark_pool_proxy_score(1_000.0, 0.0) == pytest.approx(0.0)
    assert dark_pool_proxy_score(0.0, 1_000.0) == pytest.approx(100.0)
    assert dark_pool_proxy_score(0.0, 0.0) == pytest.approx(50.0)


def _row(
    code: str,
    sequence: int,
    time: str,
    price: float,
    volume: float,
    direction: str,
) -> dict:
    return {
        "code": code,
        "sequence": sequence,
        "time": time,
        "price": price,
        "volume": volume,
        "turnover": price * volume,
        "ticker_direction": direction,
    }
