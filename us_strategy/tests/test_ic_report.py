# -*- coding: utf-8 -*-
"""Unit tests for ic_report pure aggregation / verdict logic (no DB, no OpenD)."""

import math

from us_strategy.ic_report import (
    DEFAULT_MIN_DAYS,
    FactorAgg,
    _records_by_day,
    aggregate_ic,
    verdict,
)
from us_strategy.persistence import SignalLogRecord


def _agg(mean_ic: float, ir: float, n_days: int) -> FactorAgg:
    return FactorAgg("short", 30, n_days, mean_ic, 0.1, ir, mean_ic)


def test_aggregate_empty_returns_zero_days_and_nan():
    n, mean, std, ir = aggregate_ic([])
    assert n == 0
    assert math.isnan(mean) and math.isnan(std) and math.isnan(ir)


def test_aggregate_single_day_has_no_spread():
    n, mean, std, ir = aggregate_ic([-0.12])
    assert n == 1
    assert mean == -0.12
    assert math.isnan(std) and math.isnan(ir)  # cannot estimate IR from 1 day


def test_aggregate_drops_nan_values():
    n, mean, _, _ = aggregate_ic([-0.10, float("nan"), -0.20])
    assert n == 2
    assert abs(mean - (-0.15)) < 1e-9


def test_aggregate_computes_mean_and_ir():
    n, mean, std, ir = aggregate_ic([-0.10, -0.20, -0.30])
    assert n == 3
    assert abs(mean - (-0.20)) < 1e-9
    assert abs(std - 0.1) < 1e-9  # sample std of -0.1,-0.2,-0.3
    assert abs(ir - (-2.0)) < 1e-9  # -0.2 / 0.1


def test_verdict_accumulating_when_too_few_days():
    v = verdict(_agg(-0.2, -2.0, 5), min_days=20)
    assert "积累中" in v and "5/20" in v


def test_verdict_qualified_requires_negative_ic_and_ir():
    v = verdict(_agg(-0.10, -1.5, DEFAULT_MIN_DAYS), min_days=DEFAULT_MIN_DAYS)
    assert "达标" in v


def test_verdict_flags_wrong_sign():
    v = verdict(_agg(0.10, 1.5, DEFAULT_MIN_DAYS), min_days=DEFAULT_MIN_DAYS)
    assert "符号反" in v


def test_verdict_not_qualified_when_weak():
    # enough days, but |IC| below gate -> not qualified
    v = verdict(_agg(-0.01, -0.1, DEFAULT_MIN_DAYS), min_days=DEFAULT_MIN_DAYS)
    assert v == "未达标"


def test_verdict_strong_ic_but_weak_ir_not_qualified():
    # |IC| passes but |IR| below gate -> still not qualified
    v = verdict(_agg(-0.20, -0.2, DEFAULT_MIN_DAYS), min_days=DEFAULT_MIN_DAYS)
    assert v == "未达标"


def test_records_by_day_groups_on_utc_date_prefix():
    recs = [
        SignalLogRecord(
            ts="2026-06-05T13:30:00+00:00", code="US.A", last_price=1.0, scores={}
        ),
        SignalLogRecord(
            ts="2026-06-05T19:55:00+00:00", code="US.B", last_price=2.0, scores={}
        ),
        SignalLogRecord(
            ts="2026-06-06T14:00:00+00:00", code="US.A", last_price=3.0, scores={}
        ),
    ]
    by_day = _records_by_day(recs)
    assert set(by_day.keys()) == {"2026-06-05", "2026-06-06"}
    assert len(by_day["2026-06-05"]) == 2
    assert len(by_day["2026-06-06"]) == 1


def test_records_by_day_skips_blank_ts():
    recs = [SignalLogRecord(ts="", code="US.A", last_price=1.0, scores={})]
    assert _records_by_day(recs) == {}
