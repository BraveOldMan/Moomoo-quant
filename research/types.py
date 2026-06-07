"""Shared data structures for signal research reports."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class CorrelationDiagnostics:
    """Correlation diagnostics for one factor and one forward-return target."""

    n: int
    ic: float
    p_value: float
    ci_low: float
    ci_high: float
    hac_t: float
    method: str


@dataclass(frozen=True)
class WalkForwardFoldResult:
    """Out-of-sample metrics for one time-series split."""

    fold: int
    train_start: str
    train_end: str
    test_start: str
    test_end: str
    metrics: dict[str, float]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class OptimizationCandidate:
    """One constrained optimization candidate and its validation evidence."""

    params: dict[str, Any]
    objective: float
    fold_metrics: list[dict[str, float]]
    accepted_for_research: bool

