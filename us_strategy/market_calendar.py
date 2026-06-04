# -*- coding: utf-8 -*-
"""NYSE 交易日历：计算指定年份的法定假日。"""

import calendar
from datetime import date, timedelta
from functools import lru_cache


def _nth_weekday(year: int, month: int, n: int, weekday: int) -> date:
    """返回指定月份第 n 个星期 weekday（0=周一）的日期。"""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first.replace(day=1 + offset + (n - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    """返回指定月份最后一个星期 weekday 的日期。"""
    last_day = calendar.monthrange(year, month)[1]
    last = date(year, month, last_day)
    offset = (last.weekday() - weekday) % 7
    return last.replace(day=last_day - offset)


def _easter(year: int) -> date:
    """Anonymous Gregorian 算法计算复活节日期。"""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ll = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ll) // 451
    month = (h + ll - 7 * m + 114) // 31
    day = (h + ll - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _nyse_observed(d: date) -> date:
    """若假日落在周末，NYSE 按规则调整观察日：周六→周五，周日→周一。"""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


@lru_cache(maxsize=4)
def get_nyse_holidays(year: int) -> frozenset[date]:
    """返回指定年份 NYSE 全年法定假日的观察日集合。"""
    holidays = set()

    # 元旦 — 1 月 1 日
    holidays.add(_nyse_observed(date(year, 1, 1)))
    # MLK Day — 1 月第 3 个周一
    holidays.add(_nth_weekday(year, 1, 3, 0))
    # Presidents' Day — 2 月第 3 个周一
    holidays.add(_nth_weekday(year, 2, 3, 0))
    # 耶稣受难日 Good Friday — 复活节前两天（周五）
    holidays.add(_easter(year) - timedelta(days=2))
    # 阵亡将士纪念日 Memorial Day — 5 月最后一个周一
    holidays.add(_last_weekday(year, 5, 0))
    # Juneteenth — 6 月 19 日（2022 年起）
    if year >= 2022:
        holidays.add(_nyse_observed(date(year, 6, 19)))
    # 独立日 — 7 月 4 日
    holidays.add(_nyse_observed(date(year, 7, 4)))
    # 劳工节 Labor Day — 9 月第 1 个周一
    holidays.add(_nth_weekday(year, 9, 1, 0))
    # 感恩节 Thanksgiving — 11 月第 4 个周四
    holidays.add(_nth_weekday(year, 11, 4, 3))
    # 圣诞节 — 12 月 25 日
    holidays.add(_nyse_observed(date(year, 12, 25)))

    return frozenset(holidays)


def is_trading_day(d: date) -> bool:
    """判断指定日期是否为 NYSE 交易日（非周末、非假日）。"""
    if d.weekday() >= 5:
        return False
    return d not in get_nyse_holidays(d.year)
