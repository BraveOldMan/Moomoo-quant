"""Statistical diagnostics for factor IC research."""

from __future__ import annotations

import math
import random
from collections.abc import Iterable, Sequence

from .dependencies import optional_import
from .types import CorrelationDiagnostics


def finite_pairs(xs: Sequence[float], ys: Sequence[float]) -> tuple[list[float], list[float]]:
    """Return aligned finite numeric pairs."""

    out_x: list[float] = []
    out_y: list[float] = []
    for x, y in zip(xs, ys):
        fx = float(x)
        fy = float(y)
        if math.isfinite(fx) and math.isfinite(fy):
            out_x.append(fx)
            out_y.append(fy)
    return out_x, out_y


def rank_values(values: Sequence[float]) -> list[float]:
    """Return average ranks, preserving ties."""

    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def pearson(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Compute a Pearson correlation without external dependencies."""

    x, y = finite_pairs(xs, ys)
    n = len(x)
    if n < 2:
        return float("nan")
    mx = sum(x) / n
    my = sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    vx = sum((a - mx) ** 2 for a in x)
    vy = sum((b - my) ** 2 for b in y)
    if vx <= 0 or vy <= 0:
        return float("nan")
    return cov / math.sqrt(vx * vy)


def information_coefficient(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    method: str = "spearman",
) -> float:
    """Return factor IC using Spearman rank correlation by default."""

    x, y = finite_pairs(factor, forward_returns)
    if len(x) < 2:
        return float("nan")
    if method == "spearman":
        return pearson(rank_values(x), rank_values(y))
    if method == "pearson":
        return pearson(x, y)
    raise ValueError(f"unsupported IC method: {method}")


def scipy_p_value(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    method: str = "spearman",
) -> float:
    """Return SciPy p-value for the requested correlation method."""

    stats = optional_import("scipy.stats")
    if stats is None:
        return float("nan")
    x, y = finite_pairs(factor, forward_returns)
    if len(x) < 3:
        return float("nan")
    if method == "spearman":
        result = stats.spearmanr(x, y)
    elif method == "pearson":
        result = stats.pearsonr(x, y)
    else:
        raise ValueError(f"unsupported IC method: {method}")
    p_value = getattr(result, "pvalue", result[1])
    return float(p_value)


def bootstrap_correlation_ci(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    method: str = "spearman",
    samples: int = 300,
    seed: int = 7,
    alpha: float = 0.05,
) -> tuple[float, float]:
    """Bootstrap a correlation confidence interval from aligned pairs."""

    x, y = finite_pairs(factor, forward_returns)
    n = len(x)
    if n < 3 or samples <= 0:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    vals: list[float] = []
    for _ in range(samples):
        idx = [rng.randrange(n) for _ in range(n)]
        corr = information_coefficient(
            [x[i] for i in idx],
            [y[i] for i in idx],
            method=method,
        )
        if math.isfinite(corr):
            vals.append(corr)
    if not vals:
        return float("nan"), float("nan")
    vals.sort()
    lo = vals[max(0, int((alpha / 2) * len(vals)))]
    hi = vals[min(len(vals) - 1, int((1 - alpha / 2) * len(vals)))]
    return lo, hi


def hac_t_stat(values: Sequence[float], max_lag: int | None = None) -> float:
    """Newey-West t-statistic for the mean of a serially ordered series."""

    vals = [float(x) for x in values if math.isfinite(float(x))]
    n = len(vals)
    if n < 3:
        return float("nan")
    mean = sum(vals) / n
    demeaned = [x - mean for x in vals]
    if max_lag is None:
        max_lag = max(1, int(n ** 0.25))
    gamma0 = sum(x * x for x in demeaned) / n
    long_run_var = gamma0
    for lag in range(1, min(max_lag, n - 1) + 1):
        gamma = sum(demeaned[i] * demeaned[i - lag] for i in range(lag, n)) / n
        weight = 1.0 - lag / (max_lag + 1.0)
        long_run_var += 2.0 * weight * gamma
    if long_run_var <= 0:
        return float("nan")
    se = math.sqrt(long_run_var / n)
    return mean / se if se > 0 else float("nan")


def signed_rank_products(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    method: str,
) -> list[float]:
    """Build a serial proxy series whose mean sign follows the IC sign."""

    x, y = finite_pairs(factor, forward_returns)
    if len(x) < 3:
        return []
    if method == "spearman":
        x = rank_values(x)
        y = rank_values(y)
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    sx = math.sqrt(sum((v - mx) ** 2 for v in x))
    sy = math.sqrt(sum((v - my) ** 2 for v in y))
    if sx <= 0 or sy <= 0:
        return []
    return [((a - mx) / sx) * ((b - my) / sy) for a, b in zip(x, y)]


def correlation_diagnostics(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    method: str = "spearman",
    bootstrap_samples: int = 300,
    seed: int = 7,
) -> CorrelationDiagnostics:
    """Return IC, p-value, bootstrap CI, and HAC t-stat diagnostics."""

    x, y = finite_pairs(factor, forward_returns)
    ic = information_coefficient(x, y, method=method)
    ci_low, ci_high = bootstrap_correlation_ci(
        x,
        y,
        method=method,
        samples=bootstrap_samples,
        seed=seed,
    )
    hac_t = hac_t_stat(signed_rank_products(x, y, method))
    return CorrelationDiagnostics(
        n=len(x),
        ic=ic,
        p_value=scipy_p_value(x, y, method=method),
        ci_low=ci_low,
        ci_high=ci_high,
        hac_t=hac_t,
        method=method,
    )


def quantile_returns(
    factor: Sequence[float],
    forward_returns: Sequence[float],
    n_quantiles: int = 5,
) -> dict[int, float]:
    """Group forward returns by factor quantile and return group means."""

    x, y = finite_pairs(factor, forward_returns)
    n = len(x)
    if n < n_quantiles or n_quantiles <= 1:
        return {}
    order = sorted(range(n), key=lambda i: x[i])
    buckets: dict[int, list[float]] = {q: [] for q in range(n_quantiles)}
    for rank_pos, idx in enumerate(order):
        q = min(n_quantiles - 1, rank_pos * n_quantiles // n)
        buckets[q].append(y[idx])
    return {q: sum(vals) / len(vals) for q, vals in buckets.items() if vals}


def aggregate_ic(daily_ics: Iterable[float]) -> tuple[int, float, float, float]:
    """Return n, mean IC, sample std, and IR for daily IC observations."""

    vals = [float(x) for x in daily_ics if math.isfinite(float(x))]
    n = len(vals)
    if n == 0:
        return 0, float("nan"), float("nan"), float("nan")
    mean = sum(vals) / n
    if n < 2:
        return n, mean, float("nan"), float("nan")
    var = sum((x - mean) ** 2 for x in vals) / (n - 1)
    std = math.sqrt(var)
    ir = mean / std if std > 0 else float("nan")
    return n, mean, std, ir


def sign_stability(values: Sequence[float], expected_sign: int = -1) -> float:
    """Return the share of finite values with the expected sign."""

    vals = [float(x) for x in values if math.isfinite(float(x))]
    if not vals:
        return float("nan")
    if expected_sign < 0:
        hits = sum(1 for x in vals if x < 0)
    else:
        hits = sum(1 for x in vals if x > 0)
    return hits / len(vals)

