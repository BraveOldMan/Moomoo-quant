"""Quote-context adapters for research runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import sqlite3
from typing import Any

import pandas as pd


def _ret_ok() -> int:
    try:
        import moomoo as ft

        return int(ft.RET_OK)
    except Exception:
        return 0


def _sanitize(value: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in value)


def _day_ktype() -> object:
    try:
        import moomoo as ft

        return ft.KLType.K_DAY
    except Exception:
        return None


def _day_period_type() -> object:
    try:
        import moomoo as ft

        return ft.PeriodType.DAY
    except Exception:
        return None


class CachedQuoteContext:
    """Quote-context adapter that caches OpenD responses as Parquet files."""

    def __init__(
        self,
        cache_dir: str | Path,
        quote_ctx_factory: Callable[[], Any] | None = None,
        refresh: bool = False,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._factory = quote_ctx_factory
        self._quote_ctx: Any | None = None
        self._refresh = refresh
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        """Close the underlying OpenD context when it was opened."""

        if self._quote_ctx is not None and hasattr(self._quote_ctx, "close"):
            self._quote_ctx.close()
        self._quote_ctx = None

    def request_history_kline(
        self,
        code: str,
        start: str,
        end: str,
        ktype: Any = None,
        max_count: int = 1000,
    ) -> tuple[int, pd.DataFrame, Any]:
        """Return cached historical K-line data in the moomoo SDK tuple shape."""

        key = self._path("kline", code, start, end)
        if key.exists() and not self._refresh:
            return _ret_ok(), pd.read_parquet(key), None
        ctx = self._ensure_context()
        if ktype is None:
            ktype = _day_ktype()
        ret, frame, page_key = ctx.request_history_kline(
            code,
            start=start,
            end=end,
            ktype=ktype,
            max_count=max_count,
        )
        if ret == _ret_ok() and isinstance(frame, pd.DataFrame) and not frame.empty:
            frame.to_parquet(key, index=False)
        return ret, frame, page_key

    def get_capital_flow(
        self,
        code: str,
        period_type: Any = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[int, pd.DataFrame]:
        """Return cached capital-flow data in the moomoo SDK tuple shape."""

        key = self._path("capital_flow", code, start or "", end or "")
        if key.exists() and not self._refresh:
            return _ret_ok(), pd.read_parquet(key)
        ctx = self._ensure_context()
        if period_type is None:
            period_type = _day_period_type()
        ret, frame = ctx.get_capital_flow(
            code,
            period_type=period_type,
            start=start,
            end=end,
        )
        if ret == _ret_ok() and isinstance(frame, pd.DataFrame) and not frame.empty:
            frame.to_parquet(key, index=False)
        return ret, frame

    def _path(self, kind: str, code: str, start: str, end: str) -> Path:
        return self._cache_dir / f"{kind}_{_sanitize(code)}_{start}_{end}.parquet"

    def _ensure_context(self) -> Any:
        if self._quote_ctx is None:
            if self._factory is None:
                raise RuntimeError("cache miss requires an OpenD quote context")
            self._quote_ctx = self._factory()
        return self._quote_ctx


class SQLiteQuoteContext:
    """Read-only quote-context adapter backed by the local history SQLite DB."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)

    def close(self) -> None:
        """Match OpenD quote context lifecycle; no persistent handle is held."""

    def request_history_kline(
        self,
        code: str,
        start: str,
        end: str,
        ktype: Any = None,
        max_count: int = 1000,
    ) -> tuple[int, pd.DataFrame, Any]:
        """Return daily K-line rows from `history_kline` in moomoo tuple shape."""

        _ = (ktype, max_count)
        query = """
            SELECT substr(time_key, 1, 10) AS time_key,
                   open,
                   close,
                   high,
                   low,
                   turnover,
                   turnover_rate,
                   volume,
                   _code AS code
              FROM history_kline
             WHERE _code = ?
               AND substr(time_key, 1, 10) >= ?
               AND substr(time_key, 1, 10) <= ?
             ORDER BY time_key
        """
        frame = self._read_frame(
            query,
            (code, start[:10], end[:10]),
            numeric_columns=(
                "open",
                "close",
                "high",
                "low",
                "turnover",
                "turnover_rate",
                "volume",
            ),
        )
        return _ret_ok(), frame, None

    def get_capital_flow(
        self,
        code: str,
        period_type: Any = None,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[int, pd.DataFrame]:
        """Return daily capital-flow rows from `capital_flow_day`."""

        _ = period_type
        query = """
            SELECT substr(capital_flow_item_time, 1, 10) AS time_key,
                   main_in_flow
              FROM capital_flow_day
             WHERE _code = ?
               AND substr(capital_flow_item_time, 1, 10) >= ?
               AND substr(capital_flow_item_time, 1, 10) <= ?
             ORDER BY capital_flow_item_time
        """
        frame = self._read_frame(
            query,
            (code, (start or "")[:10], (end or "9999-12-31")[:10]),
            numeric_columns=("main_in_flow",),
        )
        return _ret_ok(), frame

    def get_daily_short_volume(
        self,
        code: str,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[int, pd.DataFrame]:
        """Return daily short-volume rows from `daily_short_volume`."""

        query = """
            SELECT timestamp_str AS time_key,
                   short_percent,
                   daily_trade_avg_ratio,
                   total_shares_short,
                   volume
              FROM daily_short_volume
             WHERE _code = ?
               AND timestamp_str >= ?
               AND timestamp_str <= ?
             ORDER BY timestamp_str
        """
        frame = self._read_frame(
            query,
            (code, (start or "")[:10], (end or "9999-12-31")[:10]),
            numeric_columns=(
                "short_percent",
                "daily_trade_avg_ratio",
                "total_shares_short",
                "volume",
            ),
        )
        return _ret_ok(), frame

    def get_microstructure_daily_features(
        self,
        code: str,
        start: str | None = None,
        end: str | None = None,
    ) -> tuple[int, pd.DataFrame]:
        """Return daily derived microstructure rows for forward research."""

        query = """
            SELECT trade_date AS time_key,
                   trade_date,
                   l2_score_avg,
                   l2_score_max,
                   l2_imbalance_avg,
                   l2_danger_count,
                   dark_pool_event_count,
                   dark_pool_net_ratio,
                   dark_pool_score_max,
                   broker_snapshot_count,
                   broker_score_avg,
                   broker_score_max
              FROM microstructure_daily_features
             WHERE _code = ?
               AND trade_date >= ?
               AND trade_date <= ?
             ORDER BY trade_date
        """
        frame = self._read_frame(
            query,
            (code, (start or "")[:10], (end or "9999-12-31")[:10]),
            numeric_columns=(
                "l2_score_avg",
                "l2_score_max",
                "l2_imbalance_avg",
                "l2_danger_count",
                "dark_pool_event_count",
                "dark_pool_net_ratio",
                "dark_pool_score_max",
                "broker_snapshot_count",
                "broker_score_avg",
                "broker_score_max",
            ),
        )
        return _ret_ok(), frame

    def _read_frame(
        self,
        query: str,
        params: tuple[Any, ...],
        numeric_columns: tuple[str, ...] = (),
    ) -> pd.DataFrame:
        if not self._db_path.exists():
            return pd.DataFrame()
        try:
            with sqlite3.connect(self._db_path) as conn:
                frame = pd.read_sql_query(query, conn, params=params)
        except (sqlite3.Error, pd.errors.DatabaseError):
            return pd.DataFrame()
        for column in numeric_columns:
            if column in frame:
                frame.loc[:, column] = pd.to_numeric(frame[column], errors="coerce")
        return frame
