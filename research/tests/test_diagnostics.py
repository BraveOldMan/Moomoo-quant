"""Tests for research IC diagnostics."""

from __future__ import annotations

import math

import pytest

from research.diagnostics import (
    aggregate_ic,
    correlation_diagnostics,
    hac_t_stat,
    quantile_returns,
)


def test_correlation_diagnostics_detects_negative_ic() -> None:
    factor = [1, 2, 3, 4, 5, 6]
    forward = [6, 5, 4, 3, 2, 1]

    diag = correlation_diagnostics(factor, forward, bootstrap_samples=50)

    assert diag.n == 6
    assert diag.ic == pytest.approx(-1.0)
    assert diag.p_value < 0.01
    assert diag.ci_low <= diag.ic <= diag.ci_high
    assert math.isfinite(diag.hac_t)


def test_hac_t_stat_returns_nan_for_small_samples() -> None:
    assert math.isnan(hac_t_stat([0.1, 0.2]))


def test_quantile_returns_orders_factor_groups() -> None:
    q = quantile_returns([1, 2, 3, 4], [0.04, 0.03, 0.02, 0.01], n_quantiles=2)
    assert q[0] > q[1]


def test_shared_aggregate_ic_matches_existing_contract() -> None:
    n, mean, std, ir = aggregate_ic([-0.10, float("nan"), -0.30])
    assert n == 2
    assert mean == pytest.approx(-0.20)
    assert std == pytest.approx(0.1414213562373095)
    assert ir == pytest.approx(-1.414213562373095)

