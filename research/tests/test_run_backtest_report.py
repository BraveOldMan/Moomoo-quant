from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from research.run_backtest_report import (
    _parse_args,
    metrics_from_result,
    render_markdown,
    write_backtest_outputs,
)


@dataclass(frozen=True)
class _Trade:
    date: str
    code: str
    side: str
    price: float
    qty: int
    commission: float = 0.0
    pnl: float = 0.0


class _Result:
    initial_cash = 100_000.0
    final_value = 110_000.0
    total_return_pct = 10.0
    benchmark_return_pct = 4.0
    alpha_pct = 6.0
    annualized_return_pct = 20.0
    max_drawdown_pct = 5.0
    sharpe = 1.4
    sortino = 2.0
    calmar = 4.0
    win_rate = 50.0
    total_commission = 2.0
    trades = [
        _Trade("2026-01-02", "US.AAPL", "BUY", 100.0, 10, 1.0, 0.0),
        _Trade("2026-01-03", "US.AAPL", "SELL", 110.0, 10, 1.0, 100.0),
    ]
    equity_curve = [100_000.0, 110_000.0]
    benchmark_curve = [100_000.0, 104_000.0]


def test_metrics_from_result_serializes_headline_values() -> None:
    metrics = metrics_from_result(
        _Result(),
        "US",
        ("US.AAPL",),
        "2026-01-01",
        "2026-01-31",
    )

    assert metrics.trade_count == 2
    assert metrics.alpha_pct == 6.0


def test_write_backtest_outputs_creates_report_package(tmp_path: Path) -> None:
    metrics = metrics_from_result(
        _Result(),
        "US",
        ("US.AAPL",),
        "2026-01-01",
        "2026-01-31",
    )

    paths = write_backtest_outputs(tmp_path, metrics, _Result(), [metrics])

    assert all(path.exists() for path in paths)
    assert "Backtest Report" in render_markdown(metrics, [metrics])
    assert "US.AAPL" in (tmp_path / "trades.csv").read_text(encoding="utf-8")


def test_parse_args_accepts_benchmark_override() -> None:
    args = _parse_args(
        [
            "--market",
            "hk",
            "--codes",
            "HK.00700",
            "--start",
            "2024-01-02",
            "--end",
            "2024-01-31",
            "--benchmark-code",
            "HK.02800",
        ]
    )

    assert args.benchmark_code == "HK.02800"
