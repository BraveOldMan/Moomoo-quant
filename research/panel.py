"""Factor-panel construction for offline signal research."""

from __future__ import annotations

from typing import Any

import pandas as pd


def build_factor_panel(
    quote_ctx: Any,
    market: Any,
    codes: list[str],
    start: str,
    end: str,
    horizon_days: int = 5,
) -> pd.DataFrame:
    """Build a historical factor panel with forward returns.

    The row at date T uses only data available at T; the forward return uses
    T+horizon as the research target.
    """

    rows: list[dict[str, float | str]] = []
    for code in codes:
        ret, frame, _ = quote_ctx.request_history_kline(
            code,
            start=start,
            end=end,
            ktype=_day_ktype(),
            max_count=1000,
        )
        if ret != 0 or frame.empty or len(frame) <= horizon_days:
            continue
        frame = _merge_capital_flow(quote_ctx, code, start, end, frame)
        frame = _merge_short_volume(quote_ctx, code, start, end, frame)
        frame = _merge_microstructure_features(quote_ctx, code, start, end, frame)
        frame = frame.sort_values("time_key").reset_index(drop=True)
        closes = [float(x) for x in frame["close"]]
        for i in range(len(frame) - horizon_days):
            close = closes[i]
            future = closes[i + horizon_days]
            if close <= 0:
                continue
            row = frame.iloc[i]
            item = {
                "date": str(row["time_key"])[:10],
                "code": code,
                "turnover": _turnover_score(market, row),
                "capital": _capital_score(market, row),
                "momentum": _momentum_score(market, closes, i),
                "short": _short_score(market, row),
                "forward_return": (future - close) / close,
            }
            item.update(_microstructure_scores(row))
            rows.append(item)
    return pd.DataFrame(rows)


def _merge_capital_flow(
    quote_ctx: Any,
    code: str,
    start: str,
    end: str,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    ret, capital = quote_ctx.get_capital_flow(code, period_type=None, start=start, end=end)
    if ret != 0 or capital.empty:
        return frame
    capital = capital.rename(columns={"capital_flow_item_time": "time_key"})
    keep = [col for col in ("time_key", "main_in_flow") if col in capital.columns]
    if len(keep) < 2:
        return frame
    return frame.merge(capital[keep], on="time_key", how="left")


def _merge_short_volume(
    quote_ctx: Any,
    code: str,
    start: str,
    end: str,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    if not hasattr(quote_ctx, "get_daily_short_volume"):
        return frame
    try:
        ret, short = quote_ctx.get_daily_short_volume(code, start=start, end=end)
    except TypeError:
        ret, short = quote_ctx.get_daily_short_volume(code)
    if ret != 0 or short.empty:
        return frame
    short = short.rename(columns={"timestamp_str": "time_key"})
    keep = [col for col in ("time_key", "short_percent") if col in short.columns]
    if len(keep) < 2:
        return frame
    short = short.loc[:, keep].copy()
    short = short.assign(time_key=short["time_key"].astype(str).str[:10])
    return frame.merge(short, on="time_key", how="left")


def _merge_microstructure_features(
    quote_ctx: Any,
    code: str,
    start: str,
    end: str,
    frame: pd.DataFrame,
) -> pd.DataFrame:
    if not hasattr(quote_ctx, "get_microstructure_daily_features"):
        return frame
    ret, micro = quote_ctx.get_microstructure_daily_features(code, start=start, end=end)
    if ret != 0 or micro.empty:
        return frame
    keep = [
        col
        for col in (
            "time_key",
            "l2_score_avg",
            "l2_score_max",
            "dark_pool_event_count",
            "dark_pool_score_max",
            "broker_snapshot_count",
            "broker_score_avg",
            "broker_score_max",
        )
        if col in micro.columns
    ]
    if len(keep) < 2:
        return frame
    micro = micro.loc[:, keep].copy()
    micro = micro.assign(time_key=micro["time_key"].astype(str).str[:10])
    return frame.merge(micro, on="time_key", how="left")


def _day_ktype() -> object:
    try:
        import moomoo as ft

        return ft.KLType.K_DAY
    except Exception:
        return None


def _turnover_score(market: Any, row: pd.Series) -> float:
    rate = float(row.get("turnover_rate") or 0.0) * 100.0
    cfg = market.config
    warning = getattr(cfg, "general_turnover_warning", cfg.turnover_warning)
    danger = getattr(cfg, "general_turnover_danger", cfg.turnover_danger)
    return float(market.features.turnover_score(rate, warning, danger))


def _capital_score(market: Any, row: pd.Series) -> float:
    turnover = float(row.get("turnover") or 0.0)
    flow = float(row.get("main_in_flow") or 0.0)
    return float(market.features.capital_flow_score(flow, turnover))


def _momentum_score(market: Any, closes: list[float], idx: int) -> float:
    bars = min(int(market.config.momentum_bars), idx + 1)
    if bars < 2:
        return 50.0
    first = closes[idx - bars + 1]
    last = closes[idx]
    if first <= 0:
        return 50.0
    return float(market.features.momentum_score((last - first) / first))


def _short_score(market: Any, row: pd.Series) -> float:
    value = row.get("short_percent")
    if pd.isna(value):
        return 50.0
    return float(market.features.short_volume_score(float(value)))


def _microstructure_scores(row: pd.Series) -> dict[str, float]:
    scores: dict[str, float] = {}
    if "l2_score_avg" in row or "l2_score_max" in row:
        scores["l2_imbalance"] = _first_finite(row, "l2_score_avg", "l2_score_max")
    if "dark_pool_score_max" in row:
        scores["dark_pool_proxy"] = _first_finite(row, "dark_pool_score_max")
    if "broker_score_avg" in row or "broker_score_max" in row:
        scores["broker"] = _first_finite(row, "broker_score_avg", "broker_score_max")
    return scores


def _first_finite(row: pd.Series, *columns: str) -> float:
    for column in columns:
        value = row.get(column)
        if not pd.isna(value):
            return float(value)
    return 50.0
