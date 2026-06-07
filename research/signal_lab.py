"""Unified US/HK signal research CLI."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pandas as pd

from .cache import CachedQuoteContext
from .diagnostics import correlation_diagnostics, quantile_returns, sign_stability
from .market import load_market
from .optimization import run_optuna_search
from .panel import build_factor_panel
from .reporting import (
    write_json,
    write_optuna_outputs,
    write_quantstats_html,
    write_summary,
    write_walkforward_outputs,
)
from .vectorbt_scan import run_vectorbt_grid
from .walkforward import run_walk_forward

DEFAULT_STEPS = ("ic", "walkforward")
CORE_FACTORS = ("capital", "turnover", "momentum")


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for signal research."""

    args = _parse_args(argv)
    steps = _parse_steps(args.steps)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    market = load_market(args.market)
    codes = _parse_codes(args.codes)
    warnings: list[str] = []
    quote_ctx = CachedQuoteContext(
        args.cache_dir,
        quote_ctx_factory=lambda: _open_quote_context(market.config),
        refresh=args.refresh_cache,
    )
    try:
        panel = build_factor_panel(
            quote_ctx,
            market,
            codes,
            args.start,
            args.end,
            horizon_days=args.horizon_days,
        )
        if panel.empty:
            raise RuntimeError("factor panel is empty; check codes, dates, OpenD, or cache")
        if "ic" in steps:
            _write_ic_outputs(output_dir, panel)
        if "walkforward" in steps:
            folds = run_walk_forward(
                market,
                quote_ctx,
                codes,
                panel,
                n_splits=args.n_splits,
                min_trades=args.min_trades,
            )
            write_walkforward_outputs(output_dir, folds)
            warnings.extend(
                f"fold {fold.fold}: {','.join(fold.warnings)}"
                for fold in folds
                if fold.warnings
            )
        if "optuna" in steps:
            best, candidates = run_optuna_search(
                market,
                quote_ctx,
                codes,
                sorted(panel["date"].astype(str).unique().tolist()),
                n_trials=args.n_trials,
                n_splits=args.n_splits,
            )
            write_optuna_outputs(output_dir, best, candidates)
            if not best.accepted_for_research:
                warnings.append("best optuna candidate failed research acceptance gates")
        if "quantstats" in steps:
            result = market.backtest.BacktestEngine(quote_ctx, market.config).run(
                codes, args.start, args.end
            )
            write_quantstats_html(
                output_dir / "quantstats.html",
                result.equity_curve,
                result.benchmark_curve,
            )
        if "vectorbt" in steps:
            run_vectorbt_grid(
                quote_ctx,
                codes,
                args.start,
                args.end,
                output_dir / "vectorbt_grid.csv",
            )
        write_summary(output_dir, market.market, codes, steps, warnings)
    finally:
        quote_ctx.close()
    return 0


def _write_ic_outputs(output_dir: Path, panel: pd.DataFrame) -> None:
    rows: list[dict[str, Any]] = []
    for factor in CORE_FACTORS:
        diag = correlation_diagnostics(
            panel[factor].astype(float).tolist(),
            panel["forward_return"].astype(float).tolist(),
        )
        quantiles = quantile_returns(
            panel[factor].astype(float).tolist(),
            panel["forward_return"].astype(float).tolist(),
        )
        rows.append(
            {
                "factor": factor,
                "n": diag.n,
                "ic": diag.ic,
                "p_value": diag.p_value,
                "ci_low": diag.ci_low,
                "ci_high": diag.ci_high,
                "hac_t": diag.hac_t,
                "method": diag.method,
                "sign_stability": sign_stability([diag.ic], expected_sign=-1),
                **{f"q{q}_return": value for q, value in quantiles.items()},
            }
        )
    frame = pd.DataFrame(rows)
    frame.to_csv(output_dir / "ic_diagnostics.csv", index=False, encoding="utf-8")
    write_json(output_dir / "ic_diagnostics.json", rows)


def _open_quote_context(config: Any) -> Any:
    import moomoo as ft

    return ft.OpenQuoteContext(host=config.host, port=config.port)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True, choices=("us", "hk"))
    parser.add_argument("--codes", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--steps", default=",".join(DEFAULT_STEPS))
    parser.add_argument("--cache-dir", default="data/research_cache")
    parser.add_argument("--output-dir", default="report/outputs/signal_research")
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--horizon-days", type=int, default=5)
    parser.add_argument("--n-splits", type=int, default=3)
    parser.add_argument("--n-trials", type=int, default=20)
    parser.add_argument("--min-trades", type=int, default=30)
    return parser.parse_args(argv)


def _parse_codes(raw: str) -> list[str]:
    codes = [code.strip() for code in raw.split(",") if code.strip()]
    if not codes:
        raise ValueError("--codes must contain at least one symbol")
    return codes


def _parse_steps(raw: str) -> list[str]:
    allowed = {"ic", "walkforward", "optuna", "quantstats", "vectorbt"}
    steps = [step.strip() for step in raw.split(",") if step.strip()]
    unknown = sorted(set(steps) - allowed)
    if unknown:
        raise ValueError(f"unsupported research steps: {unknown}")
    return steps or list(DEFAULT_STEPS)


if __name__ == "__main__":
    raise SystemExit(main())

