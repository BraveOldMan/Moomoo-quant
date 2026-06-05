# -*- coding: utf-8 -*-
"""HKEX 交易日历（港股）。

港股假日含大量农历节日（春节/清明/佛诞/端午/中秋/重阳），无法像复活节那样用
规则推算，故采用 **混合方案**：

1. **生产首选 API**：`refresh_trading_days_from_api(quote_ctx, ...)` 调
   `request_trading_days(Market.HK, ...)` 从 OpenD 取真实交易日并缓存；
   `is_trading_day` 命中缓存即以其为准（最准确、免维护）。
2. **离线兜底**：未刷新 API 时回退到下方硬编码 `_HKEX_HOLIDAYS` 表（保证单测
   无需 OpenD）。

⚠️ 硬编码表为**人工录入、需逐年核对**官方历（https://www.hkex.com.hk）。固定公历
假日（元旦/劳动节/回归日/国庆/圣诞）可靠；农历假日（标注 *）务必核对。2026/2027
为最佳估计，上线前请用 API 覆盖或人工校正。
"""

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)

# 硬编码 HKEX 全年休市日（交易所实际闭市的工作日；已含周末顺延观察日）。
# * = 农历/复活节相关，需逐年核对。
_HKEX_HOLIDAYS: dict[int, frozenset[date]] = {
    2025: frozenset(
        {
            date(2025, 1, 1),  # 元旦
            date(2025, 1, 29),  # *农历新年初一
            date(2025, 1, 30),  # *农历新年初二
            date(2025, 1, 31),  # *农历新年初三
            date(2025, 4, 4),  # *清明节
            date(2025, 4, 18),  # *耶稣受难日
            date(2025, 4, 21),  # *复活节后星期一
            date(2025, 5, 1),  # 劳动节
            date(2025, 5, 5),  # *佛诞
            date(2025, 7, 1),  # 香港特别行政区成立纪念日
            date(2025, 10, 1),  # 国庆节
            date(2025, 10, 7),  # *中秋节翌日
            date(2025, 10, 29),  # *重阳节
            date(2025, 12, 25),  # 圣诞节
            date(2025, 12, 26),  # 圣诞节翌日
        }
    ),
    2026: frozenset(
        {
            date(2026, 1, 1),  # 元旦
            date(2026, 2, 17),  # *农历新年初一
            date(2026, 2, 18),  # *农历新年初二
            date(2026, 2, 19),  # *农历新年初三
            date(2026, 4, 3),  # *耶稣受难日
            date(2026, 4, 6),  # *复活节后星期一/清明顺延
            date(2026, 5, 1),  # 劳动节
            date(2026, 5, 25),  # *佛诞（5/24 周日顺延）
            date(2026, 6, 19),  # *端午节
            date(2026, 7, 1),  # 回归纪念日
            date(2026, 9, 25),  # *中秋节翌日
            date(2026, 10, 1),  # 国庆节
            date(2026, 10, 19),  # *重阳节（10/18 周日顺延）
            date(2026, 12, 25),  # 圣诞节
            date(2026, 12, 28),  # 圣诞节翌日（12/26 周六顺延至周一）
        }
    ),
    2027: frozenset(
        {
            date(2027, 1, 1),  # 元旦
            date(2027, 2, 8),  # *农历新年（2/6 周六起顺延）
            date(2027, 2, 9),  # *农历新年
            date(2027, 2, 10),  # *农历新年
            date(2027, 3, 26),  # *耶稣受难日
            date(2027, 3, 29),  # *复活节后星期一
            date(2027, 4, 5),  # *清明节
            date(2027, 5, 1),  # 劳动节（注：周六，仅占位，交易所本就休市）
            date(2027, 5, 13),  # *佛诞
            date(2027, 6, 9),  # *端午节
            date(2027, 7, 1),  # 回归纪念日
            date(2027, 9, 16),  # *中秋节翌日
            date(2027, 10, 1),  # 国庆节
            date(2027, 10, 8),  # *重阳节
            date(2027, 12, 27),  # 圣诞节翌日（顺延）
        }
    ),
}

# API 取回的交易日缓存（date 集合）；非空时优先于硬编码表。
_API_TRADING_DAYS: set[date] = set()


def get_hkex_holidays(year: int) -> frozenset[date]:
    """返回指定年份 HKEX 硬编码休市日集合；缺该年则告警并返回空集。"""
    holidays = _HKEX_HOLIDAYS.get(year)
    if holidays is None:
        logger.warning(
            "HKEX 硬编码假日表缺少 %d 年，请补表或用 API 刷新；暂按仅周末处理。",
            year,
        )
        return frozenset()
    return holidays


def _parse_api_day(item) -> date | None:
    """从 request_trading_days 的返回项解析出 date。

    OpenD 返回形如 [{'time': '2024-01-02', 'trade_date_type': 'WHOLE'}, ...]
    的列表；旧版本可能直接是日期字符串。两种都兼容。
    """
    raw = None
    if isinstance(item, dict):
        raw = item.get("time") or item.get("trade_date") or item.get("date")
    elif isinstance(item, str):
        raw = item
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def refresh_trading_days_from_api(quote_ctx, start: str, end: str) -> int:
    """用 moomoo request_trading_days 刷新 HKEX 交易日缓存。返回写入的交易日数。

    失败（OpenD 不可用/返回异常）时静默返回 0，调用方继续走硬编码兜底。
    """
    import moomoo as ft

    try:
        ret, data = quote_ctx.request_trading_days(
            market=ft.Market.HK, start=start, end=end
        )
    except Exception as exc:  # 网络/网关异常不应阻断主流程
        logger.warning("request_trading_days 调用失败，回退硬编码日历: %s", exc)
        return 0
    if ret != ft.RET_OK or not data:
        logger.warning("request_trading_days 返回异常，回退硬编码日历: %s", data)
        return 0
    days = {d for d in (_parse_api_day(x) for x in data) if d is not None}
    if days:
        _API_TRADING_DAYS.update(days)
    return len(days)


def is_trading_day(d: date) -> bool:
    """判断是否为 HKEX 交易日（非周末、非假日）。

    优先用 API 缓存（命中所属年份即以其为准）；否则回退硬编码假日表。
    """
    if d.weekday() >= 5:  # 周末
        return False
    if _API_TRADING_DAYS:
        # 仅当缓存覆盖该年份时才信任缓存（避免越界年份误判全休市）。
        if any(day.year == d.year for day in _API_TRADING_DAYS):
            return d in _API_TRADING_DAYS
    return d not in get_hkex_holidays(d.year)
