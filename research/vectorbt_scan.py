"""Independent vectorbt grid scan for research acceleration only."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .dependencies import require_optional


def run_vectorbt_grid(
    quote_ctx: Any,
    codes: list[str],
    start: str,
    end: str,
    output_path: str | Path,
) -> pd.DataFrame:
    """Run a simple momentum grid with vectorbt and write CSV results."""

    vectorbt = require_optional("vectorbt")
    close = _close_matrix(quote_ctx, codes, start, end)
    rows: list[dict[str, float | int]] = []
    if close.empty:
        frame = pd.DataFrame(rows)
        frame.to_csv(output_path, index=False, encoding="utf-8")
        return frame
    for lookback in (3, 5, 10):
        momentum = close.pct_change(lookback)
        for entry_z in (0.01, 0.02, 0.03):
            entries = momentum > entry_z
            exits = momentum < 0
            portfolio = vectorbt.Portfolio.from_signals(
                close,
                entries,
                exits,
                init_cash=100_000.0,
                fees=0.001,
                freq="1D",
            )
            rows.append(
                {
                    "lookback": lookback,
                    "entry_z": entry_z,
                    "total_return_pct": float(portfolio.total_return().mean() * 100),
                    "max_drawdown_pct": float(portfolio.max_drawdown().mean() * 100),
                    "sharpe": float(portfolio.sharpe_ratio().mean()),
                }
            )
    frame = pd.DataFrame(rows)
    frame.to_csv(output_path, index=False, encoding="utf-8")
    return frame


def _close_matrix(quote_ctx: Any, codes: list[str], start: str, end: str) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for code in codes:
        ret, frame, _ = quote_ctx.request_history_kline(code, start=start, end=end)
        if ret != 0 or frame.empty:
            continue
        frames.append(frame[["time_key", "close"]].assign(code=code))
    if not frames:
        return pd.DataFrame()
    all_data = pd.concat(frames, ignore_index=True)
    return all_data.pivot(index="time_key", columns="code", values="close").sort_index()

