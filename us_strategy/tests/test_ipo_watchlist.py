# -*- coding: utf-8 -*-
"""IPO watchlist persistence tests."""

from datetime import date, timedelta

from ipo_watchlist import IpoWatchRecord, append_today_records, load_today_records


def test_ipo_watchlist_appends_dedupes_and_loads_only_today(tmp_path):
    path = tmp_path / "ipo_watchlist.txt"
    today = date(2026, 6, 9)
    yesterday = today - timedelta(days=1)

    first = IpoWatchRecord(today, "US.WHK", "WhiteHawk", "2026-06-09")
    old = IpoWatchRecord(yesterday, "US.OLD", "Old Co", "2026-06-08")

    assert append_today_records(path, {"US.WHK": first, "US.OLD": old}) == [
        old,
        first,
    ]
    assert append_today_records(path, {"US.WHK": first}) == []

    loaded = load_today_records(path, today)

    assert list(loaded) == ["US.WHK"]
    assert loaded["US.WHK"].name == "WhiteHawk"
    assert len(path.read_text(encoding="utf-8").splitlines()) == 2
