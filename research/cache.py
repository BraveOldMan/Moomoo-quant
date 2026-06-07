"""OpenD-backed Parquet cache for research runs."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
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
