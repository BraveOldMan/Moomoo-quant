"""Constrained Optuna search for research-only parameter candidates."""

from __future__ import annotations

import dataclasses
import statistics
from typing import Any

from .dependencies import require_optional
from .types import OptimizationCandidate, WalkForwardFoldResult
from .walkforward import make_time_splits


def score_fold_metrics(metrics: list[dict[str, float]]) -> float:
    """Objective: median Sharpe + alpha bonus - drawdown penalty."""

    if not metrics:
        return float("-inf")
    sharpes = [m["sharpe"] for m in metrics]
    alphas = [m["alpha_pct"] for m in metrics]
    drawdowns = [m["max_drawdown_pct"] for m in metrics]
    return (
        statistics.median(sharpes)
        + 0.01 * statistics.median(alphas)
        - 0.05 * statistics.median(drawdowns)
    )


def accepted_for_research(
    fold_metrics: list[dict[str, float]],
    max_drawdown_pct: float = 25.0,
    min_trades: int = 30,
) -> bool:
    """Return whether a candidate clears minimum research gates."""

    if not fold_metrics:
        return False
    for metrics in fold_metrics:
        if metrics["max_drawdown_pct"] > max_drawdown_pct:
            return False
        if metrics["trade_count"] < min_trades:
            return False
    return True


def run_optuna_search(
    market: Any,
    quote_ctx: Any,
    codes: list[str],
    dates: list[str],
    n_trials: int = 20,
    n_splits: int = 3,
) -> tuple[OptimizationCandidate, list[OptimizationCandidate]]:
    """Run constrained Optuna search without writing strategy defaults."""

    optuna = require_optional("optuna")
    splits = make_time_splits(dates, n_splits=n_splits)
    if not splits:
        raise RuntimeError("not enough dates for optuna walk-forward validation")
    candidates: list[OptimizationCandidate] = []

    def objective(trial: Any) -> float:
        params = {
            "w_turnover": trial.suggest_float("w_turnover", 0.05, 0.50),
            "w_capital": trial.suggest_float("w_capital", 0.05, 0.80),
            "w_momentum": trial.suggest_float("w_momentum", 0.05, 0.50),
            "buy_threshold": trial.suggest_float("buy_threshold", 25.0, 45.0),
            "sell_threshold": trial.suggest_float("sell_threshold", 50.0, 75.0),
            "stop_loss_pct": trial.suggest_float("stop_loss_pct", 0.02, 0.10),
            "trailing_stop_pct": trial.suggest_float("trailing_stop_pct", 0.03, 0.15),
            "use_atr_sizing": trial.suggest_categorical("use_atr_sizing", [False, True]),
            "atr_stop_multiple": trial.suggest_float("atr_stop_multiple", 1.0, 3.0),
            "atr_risk_per_trade_pct": trial.suggest_float(
                "atr_risk_per_trade_pct", 0.005, 0.02
            ),
        }
        cfg = dataclasses.replace(market.config, **params)
        engine = market.backtest.BacktestEngine(quote_ctx, cfg)
        fold_metrics: list[dict[str, float]] = []
        for _, test_dates in splits:
            result = engine.run(codes, test_dates[0], test_dates[-1])
            fold_metrics.append(
                {
                    "sharpe": result.sharpe,
                    "alpha_pct": result.alpha_pct,
                    "max_drawdown_pct": result.max_drawdown_pct,
                    "trade_count": float(len(result.trades)),
                }
            )
        score = score_fold_metrics(fold_metrics)
        candidates.append(
            OptimizationCandidate(
                params=params,
                objective=score,
                fold_metrics=fold_metrics,
                accepted_for_research=accepted_for_research(fold_metrics),
            )
        )
        return score

    study = optuna.create_study(direction="maximize")
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    best = max(candidates, key=lambda c: c.objective)
    return best, candidates


def fold_result_to_metrics(result: WalkForwardFoldResult) -> dict[str, float]:
    """Extract metrics from a walk-forward result."""

    return dict(result.metrics)

