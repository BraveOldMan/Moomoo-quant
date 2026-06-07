from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from .cache import CachedQuoteContext, SQLiteQuoteContext
from .market import load_market


DEFAULT_OUTPUT_DIR = "report/outputs/backtest_report"


@dataclass(frozen=True)
class BacktestMetrics:
    """Serializable headline metrics from BacktestResult."""

    market: str
    codes: tuple[str, ...]
    start: str
    end: str
    initial_cash: float
    final_value: float
    total_return_pct: float
    benchmark_return_pct: float
    alpha_pct: float
    annualized_return_pct: float
    max_drawdown_pct: float
    sharpe: float
    sortino: float
    calmar: float
    trade_count: int
    win_rate: float
    total_commission: float


def metrics_from_result(
    result: Any,
    market: str,
    codes: tuple[str, ...],
    start: str,
    end: str,
) -> BacktestMetrics:
    """Convert an existing BacktestResult-like object into report metrics."""

    return BacktestMetrics(
        market=market,
        codes=codes,
        start=start,
        end=end,
        initial_cash=float(result.initial_cash),
        final_value=float(result.final_value),
        total_return_pct=float(result.total_return_pct),
        benchmark_return_pct=float(result.benchmark_return_pct),
        alpha_pct=float(result.alpha_pct),
        annualized_return_pct=float(result.annualized_return_pct),
        max_drawdown_pct=float(result.max_drawdown_pct),
        sharpe=float(result.sharpe),
        sortino=float(result.sortino),
        calmar=float(result.calmar),
        trade_count=len(result.trades),
        win_rate=float(result.win_rate),
        total_commission=float(result.total_commission),
    )


def write_backtest_outputs(
    output_dir: Path,
    metrics: BacktestMetrics,
    result: Any,
    walkforward_metrics: list[BacktestMetrics],
) -> tuple[Path, Path, Path, Path]:
    """Write Markdown, JSON, trades CSV, and walk-forward CSV outputs."""

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "backtest_report.md"
    metrics_path = output_dir / "backtest_metrics.json"
    trades_path = output_dir / "trades.csv"
    walkforward_path = output_dir / "walkforward.csv"

    metrics_path.write_text(
        json.dumps(
            {
                "metrics": asdict(metrics),
                "walkforward": [asdict(item) for item in walkforward_metrics],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    report_path.write_text(
        render_markdown(metrics, walkforward_metrics),
        encoding="utf-8",
    )
    _write_trades_csv(trades_path, result.trades)
    _write_walkforward_csv(walkforward_path, walkforward_metrics)
    return report_path, metrics_path, trades_path, walkforward_path


def render_markdown(
    metrics: BacktestMetrics,
    walkforward_metrics: list[BacktestMetrics],
) -> str:
    """Render a compact Markdown backtest report."""

    lines = [
        "# Backtest Report",
        "",
        f"- market: {metrics.market}",
        f"- codes: {', '.join(metrics.codes)}",
        f"- window: {metrics.start} .. {metrics.end}",
        "",
        "| metric | value |",
        "|---|---:|",
        f"| final_value | {metrics.final_value:.2f} |",
        f"| total_return_pct | {metrics.total_return_pct:.2f} |",
        f"| benchmark_return_pct | {metrics.benchmark_return_pct:.2f} |",
        f"| alpha_pct | {metrics.alpha_pct:.2f} |",
        f"| annualized_return_pct | {metrics.annualized_return_pct:.2f} |",
        f"| max_drawdown_pct | {metrics.max_drawdown_pct:.2f} |",
        f"| sharpe | {metrics.sharpe:.2f} |",
        f"| sortino | {metrics.sortino:.2f} |",
        f"| calmar | {metrics.calmar:.2f} |",
        f"| trade_count | {metrics.trade_count} |",
        f"| win_rate | {metrics.win_rate:.2f} |",
        f"| total_commission | {metrics.total_commission:.2f} |",
        "",
        "## Walk Forward",
        "",
        "| fold | start | end | sharpe | max_drawdown_pct | trade_count |",
        "|---:|---|---|---:|---:|---:|",
    ]
    for index, item in enumerate(walkforward_metrics, start=1):
        lines.append(
            f"| {index} | {item.start} | {item.end} | {item.sharpe:.2f} | "
            f"{item.max_drawdown_pct:.2f} | {item.trade_count} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Run the existing BacktestEngine and write a report package."""

    args = _parse_args(argv)
    market = load_market(args.market)
    if args.benchmark_code:
        market = replace(
            market,
            config=replace(market.config, backtest_benchmark=args.benchmark_code),
            default_benchmark=args.benchmark_code,
        )
    codes = tuple(_parse_codes(args.codes))
    output_dir = Path(args.output_dir)
    quote_ctx = _make_quote_context(args, market.config)
    try:
        engine = market.backtest.BacktestEngine(quote_ctx, market.config)
        result = engine.run(list(codes), args.start, args.end)
        if not result.equity_curve:
            raise RuntimeError("backtest produced an empty equity curve")
        walkforward = engine.run_walk_forward(
            list(codes),
            args.start,
            args.end,
            n_splits=args.n_splits,
        )
        walkforward_metrics = [
            metrics_from_result(
                fold,
                market.market,
                codes,
                args.start,
                args.end,
            )
            for fold in walkforward
            if fold.equity_curve
        ]
        metrics = metrics_from_result(result, market.market, codes, args.start, args.end)
        write_backtest_outputs(output_dir, metrics, result, walkforward_metrics)
    finally:
        quote_ctx.close()
    print(f"wrote backtest report to {output_dir}")
    return 0


def _write_trades_csv(path: Path, trades: list[Any]) -> None:
    fields = ("date", "code", "side", "price", "qty", "commission", "pnl")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {
                    field: getattr(trade, field)
                    for field in fields
                }
            )


def _write_walkforward_csv(
    path: Path,
    metrics: list[BacktestMetrics],
) -> None:
    fields = tuple(BacktestMetrics.__dataclass_fields__)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item in metrics:
            writer.writerow(asdict(item))


def _open_quote_context(config: Any) -> Any:
    import moomoo as ft

    return ft.OpenQuoteContext(host=config.host, port=config.port)


def _make_quote_context(args: argparse.Namespace, config: Any) -> Any:
    if args.source == "sqlite":
        return SQLiteQuoteContext(args.sqlite_db)
    return CachedQuoteContext(
        args.cache_dir,
        quote_ctx_factory=lambda: _open_quote_context(config),
        refresh=args.refresh_cache,
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market", required=True, choices=("us", "hk"))
    parser.add_argument("--codes", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--source", choices=("opend", "sqlite"), default="opend")
    parser.add_argument("--sqlite-db", default="us_strategy/history_data.db")
    parser.add_argument(
        "--benchmark-code",
        default="",
        help="Optional benchmark override, e.g. HK.02800 when HK.800000 is unavailable.",
    )
    parser.add_argument("--cache-dir", default="data/research_cache")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--refresh-cache", action="store_true")
    parser.add_argument("--n-splits", type=int, default=3)
    return parser.parse_args(argv)


def _parse_codes(raw: str) -> list[str]:
    codes = [code.strip() for code in raw.split(",") if code.strip()]
    if not codes:
        raise ValueError("--codes must contain at least one symbol")
    return codes


if __name__ == "__main__":
    raise SystemExit(main())
