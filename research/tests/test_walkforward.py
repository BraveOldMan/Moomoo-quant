"""Tests for walk-forward split safety."""

from __future__ import annotations

from research.walkforward import make_time_splits


def test_time_splits_never_leak_future_dates() -> None:
    dates = [f"2025-01-{day:02d}" for day in range(1, 11)]

    splits = make_time_splits(dates, n_splits=3)

    assert splits
    for train, test in splits:
        assert train[-1] < test[0]

