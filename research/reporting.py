"""Report writers for signal research outputs."""

from __future__ import annotations

import dataclasses
import json
import warnings
from pathlib import Path
from typing import Any

import pandas as pd

from .dependencies import require_optional
from .types import OptimizationCandidate, WalkForwardFoldResult


def write_json(path: str | Path, data: Any) -> None:
    """Write JSON using dataclass-aware serialization."""

    Path(path).write_text(
        json.dumps(_jsonable(data), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_walkforward_outputs(
    output_dir: str | Path,
    rows: list[WalkForwardFoldResult],
) -> None:
    """Write walk-forward CSV and JSON outputs."""

    out = Path(output_dir)
    payload = [_jsonable(row) for row in rows]
    write_json(out / "walkforward.json", payload)
    flat = []
    for row in rows:
        item = {
            "fold": row.fold,
            "train_start": row.train_start,
            "train_end": row.train_end,
            "test_start": row.test_start,
            "test_end": row.test_end,
            "warnings": ";".join(row.warnings),
        }
        item.update(row.metrics)
        flat.append(item)
    pd.DataFrame(flat).to_csv(out / "walkforward.csv", index=False, encoding="utf-8")


def write_optuna_outputs(
    output_dir: str | Path,
    best: OptimizationCandidate,
    candidates: list[OptimizationCandidate],
) -> None:
    """Write Optuna candidate outputs."""

    out = Path(output_dir)
    write_json(out / "optuna_best.json", best)
    rows = []
    for idx, candidate in enumerate(candidates, start=1):
        row = {
            "trial": idx,
            "objective": candidate.objective,
            "accepted_for_research": candidate.accepted_for_research,
        }
        row.update(candidate.params)
        rows.append(row)
    pd.DataFrame(rows).to_csv(out / "optuna_trials.csv", index=False, encoding="utf-8")


def write_quantstats_html(
    output_path: str | Path,
    equity_curve: list[float],
    benchmark_curve: list[float],
) -> None:
    """Write QuantStats HTML when enough returns are available."""

    output = Path(output_path)
    if len(equity_curve) < 3:
        output.write_text("<html><body>insufficient equity data</body></html>", encoding="utf-8")
        return
    quantstats = require_optional("quantstats")
    index = pd.date_range("2000-01-01", periods=len(equity_curve), freq="D")
    returns = pd.Series(equity_curve, index=index, dtype=float).pct_change().dropna()
    benchmark = None
    if len(benchmark_curve) == len(equity_curve):
        benchmark = (
            pd.Series(benchmark_curve, index=index, dtype=float).pct_change().dropna()
        )
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=FutureWarning, module="quantstats.*")
        warnings.filterwarnings("ignore", category=FutureWarning, module="seaborn.*")
        quantstats.reports.html(
            returns,
            benchmark=benchmark,
            output=str(output),
            title="Moomoo Signal Research",
        )


def write_summary(
    output_dir: str | Path,
    market: str,
    codes: list[str],
    steps: list[str],
    warnings: list[str],
) -> None:
    """Write the one-page research summary."""

    lines = [
        "# Signal Research Summary",
        "",
        f"- market: {market}",
        f"- codes: {', '.join(codes)}",
        f"- steps: {', '.join(steps)}",
        "",
        "## Warnings",
    ]
    lines.extend(f"- {w}" for w in warnings) if warnings else lines.append("- none")
    Path(output_dir, "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, list):
        return [_jsonable(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value
