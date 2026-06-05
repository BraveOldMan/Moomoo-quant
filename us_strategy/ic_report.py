# -*- coding: utf-8 -*-
"""Daily forward-IC health check for the (still weight-0) extension factors.

Runs forward_ic_from_log per factor per horizon, producing ONE IC value per
trading day, appends it to an `ic_history` table, then prints the cumulative
IC/IR evolution so you can watch which factors stabilize as samples accumulate
across trading days.

Why per-day, then aggregate (instead of pooling everything):
  - Pooling all intraday samples inflates n with overlapping / non-independent
    observations and yields no IR (no spread across periods).
  - One cross-sectional IC per day -> a distribution across days -> IR is just
    mean(daily IC) / std(daily IC). This is the standard IC/IR methodology and
    is exactly the gate the project requires before assigning non-zero weight.

A factor qualifies for a non-zero weight only when, over enough trading days:
  - |mean IC| > IC_MIN
  - |IR|      > IR_MIN
  - sign is consistent and NEGATIVE (scores are risk scores: high -> lower fwd return)

Run:  python -m us_strategy.ic_report
Env:
  IC_HORIZONS    csv of horizon minutes (default "15,30")
  IC_MIN_DAYS    days required before a verdict can be "qualified" (default 20)
  DB_PATH        sqlite path (default us_strategy/positions.db)
"""

from __future__ import annotations

import math
import os
import sqlite3
from dataclasses import dataclass

from .analysis import forward_ic_from_log
from .persistence import SignalLogRecord, SignalLogStore

# ── thresholds ──────────────────────────────────────────────────────────────
IC_MIN = 0.03  # |mean IC| gate
IR_MIN = 0.5  # |IR| gate
MIN_PAIRS_PER_DAY = 10  # a day counts only if it yields >= this many fwd pairs
DEFAULT_MIN_DAYS = 20  # qualifying days before a verdict may read "达标"

CORE_FACTORS = ("capital", "turnover", "momentum")
EXTENSION_FACTORS = ("order_flow", "obi", "intraday_flow", "short", "option_iv")
ALL_FACTORS = CORE_FACTORS + EXTENSION_FACTORS


# ── pure aggregation (unit-tested, no DB) ───────────────────────────────────
@dataclass(frozen=True)
class FactorAgg:
    factor: str
    horizon_min: int
    n_days: int
    mean_ic: float
    std_ic: float
    ir: float
    latest_ic: float


def aggregate_ic(daily_ics: list[float]) -> tuple[int, float, float, float]:
    """Return (n_days, mean_ic, std_ic, ir) from a list of per-day IC values.

    std is the sample stddev (ddof=1); IR = mean/std. With <2 days std and IR
    are NaN (cannot estimate spread yet).
    """
    vals = [x for x in daily_ics if x == x]  # drop NaN
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


def verdict(agg: FactorAgg, min_days: int = DEFAULT_MIN_DAYS) -> str:
    """Human verdict given an aggregated factor result."""
    if agg.n_days < min_days:
        return f"积累中 ({agg.n_days}/{min_days}日)"
    if agg.mean_ic != agg.mean_ic or agg.ir != agg.ir:
        return "数据不足"
    if agg.mean_ic <= -IC_MIN and agg.ir <= -IR_MIN:
        return "✅ 达标(可赋权)"
    if agg.mean_ic >= IC_MIN and agg.ir >= IR_MIN:
        return "⚠️ 符号反(考虑反向)"
    return "未达标"


# ── persistence of the IC history ───────────────────────────────────────────
def _init_history(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ic_history (
            date        TEXT NOT NULL,
            factor      TEXT NOT NULL,
            horizon_min INTEGER NOT NULL,
            ic          REAL,
            n           INTEGER NOT NULL,
            PRIMARY KEY (date, factor, horizon_min)
        )
        """
    )


def _records_by_day(records: list[SignalLogRecord]) -> dict[str, list[SignalLogRecord]]:
    """Group by UTC date prefix of ts (one US RTH session == one UTC date)."""
    by_day: dict[str, list[SignalLogRecord]] = {}
    for r in records:
        day = (r.ts or "")[:10]
        if day:
            by_day.setdefault(day, []).append(r)
    return by_day


def compute_and_store(
    db_path: str, horizons_min: list[int]
) -> list[str]:
    """Compute per-day IC for every (day, factor, horizon) and upsert. Returns days processed."""
    store = SignalLogStore(db_path)
    by_day = _records_by_day(store.load())
    conn = sqlite3.connect(db_path)
    try:
        _init_history(conn)
        for day, recs in by_day.items():
            for h in horizons_min:
                for f in ALL_FACTORS:
                    s = forward_ic_from_log(recs, f, float(h) * 60.0)
                    ic = None if s.ic != s.ic else float(s.ic)
                    conn.execute(
                        "INSERT OR REPLACE INTO ic_history (date,factor,horizon_min,ic,n)"
                        " VALUES (?,?,?,?,?)",
                        (day, f, h, ic, s.n),
                    )
        conn.commit()
    finally:
        conn.close()
    return sorted(by_day.keys())


def load_aggregates(db_path: str, horizon_min: int) -> list[FactorAgg]:
    """Aggregate stored daily ICs (days with n>=MIN_PAIRS_PER_DAY) per factor."""
    conn = sqlite3.connect(db_path)
    try:
        _init_history(conn)
        out: list[FactorAgg] = []
        for f in ALL_FACTORS:
            rows = conn.execute(
                "SELECT date, ic, n FROM ic_history WHERE factor=? AND horizon_min=?"
                " ORDER BY date",
                (f, horizon_min),
            ).fetchall()
            daily = [r[1] for r in rows if r[1] is not None and r[2] >= MIN_PAIRS_PER_DAY]
            n_days, mean, std, ir = aggregate_ic(daily)
            latest = daily[-1] if daily else float("nan")
            out.append(FactorAgg(f, horizon_min, n_days, mean, std, ir, latest))
        return out
    finally:
        conn.close()


# ── report ──────────────────────────────────────────────────────────────────
def _fmt(x: float) -> str:
    return " nan " if x != x else f"{x:+.3f}"


def render_report(db_path: str, horizons_min: list[int], min_days: int) -> str:
    lines: list[str] = []
    for h in horizons_min:
        aggs = load_aggregates(db_path, h)
        lines.append(f"\n=== 前向IC 累计体检 @ horizon={h}min "
                     f"(达标: |IC|>{IC_MIN} 且 |IR|>{IR_MIN} 且负号; 共需≥{min_days}日) ===")
        lines.append(f"{'factor':14}{'meanIC':>8}{'IR':>8}{'latest':>8}{'n_days':>7}  verdict")
        for a in sorted(aggs, key=lambda x: (x.mean_ic if x.mean_ic == x.mean_ic else 9)):
            tag = "  " if a.factor in CORE_FACTORS else "* "  # * = 扩展因子
            lines.append(
                f"{tag}{a.factor:12}{_fmt(a.mean_ic):>8}{_fmt(a.ir):>8}"
                f"{_fmt(a.latest_ic):>8}{a.n_days:>7}  {verdict(a, min_days)}"
            )
    lines.append("\n注: * = 扩展因子(当前权重0)。核心因子(capital/turnover/momentum)仅作参照。")
    return "\n".join(lines)


def run() -> None:
    db_path = os.environ.get("DB_PATH", "us_strategy/positions.db")
    horizons = [int(x) for x in os.environ.get("IC_HORIZONS", "15,30").split(",") if x.strip()]
    min_days = int(os.environ.get("IC_MIN_DAYS", str(DEFAULT_MIN_DAYS)))
    days = compute_and_store(db_path, horizons)
    print(f"已处理 {len(days)} 个交易日: {days[0]} .. {days[-1]}" if days else "signal_log 无数据")
    print(render_report(db_path, horizons, min_days))


if __name__ == "__main__":
    run()
