# -*- coding: utf-8 -*-
"""市场时区日期工具。

美股策略运行在中国机器上时，必须按纽约市场日判断交易日、PDT 和日内数据。
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
