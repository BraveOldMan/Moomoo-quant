"""Tests for research report writers with synthetic data."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from research.reporting import write_quantstats_html
from research.vectorbt_scan import run_vectorbt_grid


class _FakeQuoteContext:
    def request_history_kline(self, code: str, start: str, end: str, **_kwargs):
        frame = pd.DataFrame(
            {
                "time_key": pd.date_range(start=start, end=end, freq="D").strftime(
                    "%Y-%m-%d"
                ),
                "close": [10.0, 10.5, 10.2, 11.0, 11.5],
            }
        )
        return 0, frame, None


class _FakePortfolio:
    @classmethod
    def from_signals(cls, *_args, **_kwargs):
        return cls()

    def total_return(self):
        return pd.Series([0.10])

    def max_drawdown(self):
        return pd.Series([0.05])

    def sharpe_ratio(self):
        return pd.Series([1.50])


def test_quantstats_writer_uses_synthetic_module(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "quantstats.html"

    def _html(_returns, benchmark=None, output=None, title=None):
        Path(output).write_text(f"<html>{title}</html>", encoding="utf-8")

    monkeypatch.setitem(
        sys.modules,
        "quantstats",
        SimpleNamespace(reports=SimpleNamespace(html=_html)),
    )

    write_quantstats_html(output, [100.0, 101.0, 102.0], [100.0, 100.5, 101.0])

    assert "Moomoo Signal Research" in output.read_text(encoding="utf-8")


def test_vectorbt_grid_writes_csv_with_synthetic_module(
    tmp_path: Path, monkeypatch
) -> None:
    output = tmp_path / "vectorbt_grid.csv"
    monkeypatch.setitem(
        sys.modules,
        "vectorbt",
        SimpleNamespace(Portfolio=_FakePortfolio),
    )

    frame = run_vectorbt_grid(
        _FakeQuoteContext(),
        ["US.A"],
        "2025-01-01",
        "2025-01-05",
        output,
    )

    assert output.exists()
    assert not frame.empty

