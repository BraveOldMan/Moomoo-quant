"""Tests for constrained Optuna research search."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from research.optimization import (
    accepted_for_research,
    run_optuna_search,
    score_fold_metrics,
)


@dataclass(frozen=True)
class _Config:
    w_turnover: float = 0.25
    w_capital: float = 0.55
    w_momentum: float = 0.20
    buy_threshold: float = 35.0
    sell_threshold: float = 60.0
    stop_loss_pct: float = 0.05
    trailing_stop_pct: float = 0.08
    use_atr_sizing: bool = False
    atr_stop_multiple: float = 2.0
    atr_risk_per_trade_pct: float = 0.01


class _Result:
    sharpe = 1.2
    alpha_pct = 3.0
    max_drawdown_pct = 8.0
    trades = [object()] * 40


class _Engine:
    def __init__(self, _ctx, _cfg) -> None:
        pass

    def run(self, _codes, _start, _end) -> _Result:
        return _Result()


def test_score_candidate_penalizes_drawdown() -> None:
    metrics = [{"sharpe": 1.0, "alpha_pct": 5.0, "max_drawdown_pct": 20.0}]
    assert score_fold_metrics(metrics) == pytest.approx(0.05)


def test_acceptance_gates_reject_low_trades() -> None:
    metrics = [{"max_drawdown_pct": 5.0, "trade_count": 3.0}]
    assert not accepted_for_research(metrics, min_trades=30)


def test_optuna_runs_three_trials_on_synthetic_engine() -> None:
    market = SimpleNamespace(
        config=_Config(),
        backtest=SimpleNamespace(BacktestEngine=_Engine),
    )
    dates = [f"2025-01-{day:02d}" for day in range(1, 12)]

    best, candidates = run_optuna_search(
        market,
        quote_ctx=None,
        codes=["US.A"],
        dates=dates,
        n_trials=3,
        n_splits=2,
    )

    assert len(candidates) == 3
    assert best.accepted_for_research
