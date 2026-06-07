from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ICGateResult:
    """Machine-readable IC eligibility gate result."""

    status: str
    eligible: bool
    reason: str


def evaluate_ic_gate(
    n_days: int,
    mean_ic: float,
    ir: float,
    min_days: int,
    ic_min: float,
    ir_min: float,
) -> ICGateResult:
    """Evaluate whether a risk-score factor may receive non-zero weight."""

    if n_days < min_days or mean_ic != mean_ic or ir != ir:
        return ICGateResult(
            status="insufficient_sample",
            eligible=False,
            reason=f"needs {min_days} valid days",
        )
    if mean_ic >= ic_min:
        return ICGateResult(
            status="failed_sign",
            eligible=False,
            reason="risk score IC is positive",
        )
    if mean_ic > -ic_min:
        return ICGateResult(
            status="failed_mean_ic",
            eligible=False,
            reason=f"|meanIC| must exceed {ic_min}",
        )
    if ir > -ir_min:
        return ICGateResult(
            status="failed_ir",
            eligible=False,
            reason=f"|IR| must exceed {ir_min} with negative sign",
        )
    return ICGateResult(
        status="eligible",
        eligible=True,
        reason="factor passed sample, meanIC, IR, and sign gates",
    )
