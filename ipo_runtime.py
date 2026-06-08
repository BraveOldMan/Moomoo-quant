# -*- coding: utf-8 -*-
"""Runtime helpers for same-day IPO strategy flow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import moomoo as ft

from ipo_watchlist import IpoWatchRecord

_DATE_COLUMNS = ("list_time", "listing_date", "ipo_date")
_CODE_COLUMNS = ("code", "stock_code")
_NAME_COLUMNS = ("name", "stock_name", "security_name", "short_name")


@dataclass(frozen=True)
class IpoCandidate:
    """Same-day IPO candidate with optional offering metadata."""

    record: IpoWatchRecord
    ipo_price_min: str = "N/A"
    ipo_price_max: str = "N/A"
    list_price: str = "N/A"
    lot_size: str = "N/A"
    issue_size: str = "N/A"

    @property
    def code(self) -> str:
        return self.record.code

    @property
    def name(self) -> str:
        return self.record.name

    @property
    def trade_date(self) -> date:
        return self.record.trade_date

    @property
    def list_time(self) -> str:
        return self.record.list_time


def candidate_from_record(record: IpoWatchRecord) -> IpoCandidate:
    """Build a candidate from a persisted watchlist record."""

    return IpoCandidate(record=record)


def fetch_today_ipos(
    data: Any,
    markets: tuple,
    target_date: date,
) -> tuple[dict[str, IpoCandidate], list[str]]:
    """Fetch IPOs whose list_time equals target_date."""

    result: dict[str, IpoCandidate] = {}
    errors: list[str] = []
    for market in markets:
        mkt = getattr(ft.Market, str(market), market)
        ret, df = data._quote.get_ipo_list(mkt)  # noqa: SLF001
        if ret != ft.RET_OK:
            errors.append(f"get_ipo_list 失败 market={market}: {df}")
            continue
        if df.empty:
            continue
        date_col = _find_column(df.columns, _DATE_COLUMNS, fuzzy=True)
        code_col = _find_column(df.columns, _CODE_COLUMNS)
        if date_col is None or code_col is None:
            errors.append(f"IPO 列表列名未识别 market={market}: {df.columns.tolist()}")
            continue
        name_col = _find_column(df.columns, _NAME_COLUMNS)
        for _, row in df.iterrows():
            listing_date = _parse_date(row.get(date_col))
            if listing_date != target_date:
                continue
            code = str(row.get(code_col, "")).strip()
            if not code:
                continue
            list_time = str(row.get(date_col, target_date.isoformat()))[:10]
            name = _row_text(row, (name_col,), code) if name_col else code
            record = IpoWatchRecord(
                trade_date=target_date,
                code=code,
                name=name,
                list_time=list_time,
            )
            result[code] = IpoCandidate(
                record=record,
                ipo_price_min=_row_text(row, ("ipo_price_min",), "N/A"),
                ipo_price_max=_row_text(row, ("ipo_price_max",), "N/A"),
                list_price=_row_text(row, ("list_price",), "N/A"),
                lot_size=_row_text(row, ("lot_size",), "N/A"),
                issue_size=_row_text(row, ("issue_size",), "N/A"),
            )
    return result, errors


def snapshot_ready(row: Any) -> bool:
    """Return True when an IPO has real tradable quote and turnover data."""

    return _row_float(row, "last_price") > 0 and _row_float(row, "turnover") > 0


def build_ipo_found_message(candidate: IpoCandidate, cfg: Any) -> str:
    """Build the Feishu message for newly discovered same-day IPOs."""

    return "\n".join(
        (
            f"标的：{candidate.name}（{candidate.code}）",
            f"上市日期：{candidate.list_time}",
            f"发行价区间：{candidate.ipo_price_min} - {candidate.ipo_price_max}",
            f"发行规模：{candidate.issue_size}",
            _ipo_profile_text(cfg),
            "状态：已加入今日 IPO 独立观察名单，等待真实行情与流动性门禁。",
        )
    )


def build_ipo_unavailable_message(
    candidate: IpoCandidate,
    row: Any,
    cfg: Any,
) -> str:
    """Build the Feishu message for IPO quote/data gates that are not ready."""

    if row is None:
        row = {}
    return "\n".join(
        (
            f"标的：{candidate.name}（{candidate.code}）",
            f"上市日期：{candidate.list_time}",
            f"最新价：{_row_float(row, 'last_price'):.3f}",
            f"成交量：{_row_float(row, 'volume'):.0f}",
            f"成交额：{_row_float(row, 'turnover'):.2f}",
            f"证券状态：{_row_text(row, ('sec_status',), 'N/A')}",
            _ipo_profile_text(cfg),
            "状态：行情或成交额未就绪，仅观察，不下单。",
        )
    )


def build_ipo_analysis_message(
    candidate: IpoCandidate,
    decision: Any,
    cfg: Any,
) -> str:
    """Build the Feishu message for the first complete IPO analysis."""

    result = getattr(decision, "result", None)
    scores = getattr(result, "scores", {}) or {}
    block_reasons = getattr(result, "buy_block_reasons", []) or []
    score_text = "，".join(f"{key}={value:.1f}" for key, value in scores.items())
    return "\n".join(
        (
            f"标的：{candidate.name}（{candidate.code}）",
            f"上市日期：{candidate.list_time}",
            f"发行价区间：{candidate.ipo_price_min} - {candidate.ipo_price_max}",
            f"发行规模：{candidate.issue_size}",
            f"最新价：{_fmt_float(getattr(result, 'last_price', None))}",
            f"成交额：{_fmt_float(_extra_value(result, 'turnover_usd'))}",
            f"换手率：{_fmt_float(getattr(result, 'turnover_rate', None))}%",
            f"流动性门禁：{'通过' if getattr(result, 'liquidity_ok', False) else '未通过'}",
            f"因子风险分：{score_text or 'N/A'}",
            f"综合风险分：{getattr(decision, 'score', 50.0):.1f}",
            f"买入阻断：{'；'.join(block_reasons) if block_reasons else '无'}",
            f"最终决策：{getattr(decision, 'signal').value} - {getattr(decision, 'reason')}",
            _ipo_profile_text(cfg),
        )
    )


def _ipo_profile_text(cfg: Any) -> str:
    return (
        "IPO风控："
        f"目标仓位{cfg.ipo_position_ratio * 100:.1f}% / "
        f"{cfg.ipo_entry_tranches}批；"
        f"止盈{cfg.ipo_take_profit_pct * 100:.1f}%；"
        f"止损{cfg.ipo_stop_loss_pct * 100:.1f}%；"
        f"峰值回撤{cfg.ipo_trailing_stop_pct * 100:.1f}%"
    )


def _find_column(
    columns: Any,
    candidates: tuple[str, ...],
    fuzzy: bool = False,
) -> str | None:
    column_list = [str(column) for column in columns]
    for candidate in candidates:
        if candidate in column_list:
            return candidate
    if fuzzy:
        for column in column_list:
            lowered = column.lower()
            if "listing" in lowered or "ipo_date" in lowered:
                return column
    return None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _row_text(row: Any, fields: tuple[str | None, ...], default: str) -> str:
    for field in fields:
        if not field:
            continue
        value = row.get(field, None)
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "n/a"}:
            return text
    return default


def _row_float(row: Any, field: str) -> float:
    try:
        return float(row.get(field, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _fmt_float(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "N/A"
    return f"{number:.3f}"


def _extra_value(result: Any, key: str) -> Any:
    extra = getattr(result, "extra", {}) or {}
    return extra.get(key)
