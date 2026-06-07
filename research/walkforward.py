"""Walk-forward validation with sklearn TimeSeriesSplit."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from typing import Any

import pandas as pd

from .dependencies import require_optional
from .diagnostics import correlation_diagnostics
from .types import WalkForwardFoldResult


def make_time_splits(dates: Sequence[str], n_splits: int = 3) -> list[tuple[list[str], list[str]]]:
    """Split ordered dates so every training window strictly precedes validation."""

    unique_dates = sorted(set(str(d)[:10] for d in dates))
    if len(unique_dates) < 3:
        return []
    n = min(n_splits, len(unique_dates) - 1)
    sklearn_model_selection = require_optional("sklearn.model_selection")
    splitter = sklearn_model_selection.TimeSeriesSplit(n_splits=n)
    out: list[tuple[list[str], list[str]]] = []
    for train_idx, test_idx in splitter.split(unique_dates):
        train = [unique_dates[i] for i in train_idx]
        test = [unique_dates[i] for i in test_idx]
        if train and test and train[-1] < test[0]:
            out.append((train, test))
    return out


def run_walk_forward(
    market: Any,
    quote_ctx: Any,
    codes: list[str],
    panel: pd.DataFrame,
    n_splits: int = 3,
    min_trades: int = 30,
) -> list[WalkForwardFoldResult]:
    """Run existing BacktestEngine over TimeSeriesSplit validation folds."""

    splits = make_time_splits(panel["date"].astype(str).tolist(), n_splits=n_splits)
    results: list[WalkForwardFoldResult] = []
    engine = market.backtest.BacktestEngine(quote_ctx, market.config)
    for idx, (train_dates, test_dates) in enumerate(splits, start=1):
        test_start = test_dates[0]
        test_end = test_dates[-1]
        result = engine.run(codes, test_start, test_end)
        test_panel = panel[panel["date"].isin(test_dates)]
        mean_ic = _mean_core_ic(test_panel)
        metrics = {
            "total_return_pct": result.total_return_pct,
            "benchmark_return_pct": result.benchmark_return_pct,
            "alpha_pct": result.alpha_pct,
            "sharpe": result.sharpe,
            "max_drawdown_pct": result.max_drawdown_pct,
            "trade_count": float(len(result.trades)),
            "mean_core_ic": mean_ic,
        }
        warnings: list[str] = []
        if len(result.trades) < min_trades:
            warnings.append("sample_size_low")
        if result.max_drawdown_pct > 25.0:
            warnings.append("drawdown_over_25pct")
        results.append(
            WalkForwardFoldResult(
                fold=idx,
                train_start=train_dates[0],
                train_end=train_dates[-1],
                test_start=test_start,
                test_end=test_end,
                metrics=metrics,
                warnings=warnings,
            )
        )
    return results


def _mean_core_ic(panel: pd.DataFrame) -> float:
    vals: list[float] = []
    for factor in (
        "capital",
        "turnover",
        "momentum",
        "short",
        "l2_imbalance",
        "dark_pool_proxy",
        "broker",
    ):
        if factor not in panel or panel.empty:
            continue
        diag = correlation_diagnostics(
            panel[factor].astype(float).tolist(),
            panel["forward_return"].astype(float).tolist(),
            bootstrap_samples=50,
        )
        if diag.ic == diag.ic:
            vals.append(diag.ic)
    return statistics.mean(vals) if vals else float("nan")
