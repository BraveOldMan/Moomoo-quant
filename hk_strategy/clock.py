# -*- coding: utf-8 -*-
"""市场时区日期工具。

港股策略运行时按香港市场日（Asia/Hong_Kong）判断交易日与日内数据；
本工具与市场无关，接受任意时区名。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


def market_datetime(timezone_name: str, now: datetime | None = None) -> datetime:
    """返回指定市场时区下的当前时间。

    now 可传入任意带时区的 datetime，用于测试或把外部时间转换到市场时区。
    """
    timezone = ZoneInfo(timezone_name)
    if now is None:
        return datetime.now(timezone)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone)
    return now.astimezone(timezone)


def market_date(timezone_name: str, now: datetime | None = None) -> date:
    """返回指定市场时区下的日期。"""
    return market_datetime(timezone_name, now=now).date()
