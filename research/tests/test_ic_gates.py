from __future__ import annotations

from research.ic_gates import evaluate_ic_gate


def test_ic_gate_requires_enough_samples() -> None:
    result = evaluate_ic_gate(5, -0.2, -1.0, 20, 0.03, 0.5)

    assert result.status == "insufficient_sample"
    assert not result.eligible


def test_ic_gate_accepts_negative_stable_factor() -> None:
    result = evaluate_ic_gate(20, -0.08, -1.2, 20, 0.03, 0.5)

    assert result.status == "eligible"
    assert result.eligible


def test_ic_gate_flags_wrong_sign() -> None:
    result = evaluate_ic_gate(20, 0.08, 1.2, 20, 0.03, 0.5)

    assert result.status == "failed_sign"


def test_ic_gate_flags_weak_mean_ic_before_ir() -> None:
    result = evaluate_ic_gate(20, -0.01, -2.0, 20, 0.03, 0.5)

    assert result.status == "failed_mean_ic"


def test_ic_gate_flags_weak_ir() -> None:
    result = evaluate_ic_gate(20, -0.08, -0.2, 20, 0.03, 0.5)

    assert result.status == "failed_ir"
