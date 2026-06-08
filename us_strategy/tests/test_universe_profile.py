# -*- coding: utf-8 -*-
"""通用化 universe 与换手率 profile 单测（无需 OpenD）。"""

from datetime import date

from us_strategy.config import StrategyConfig
from us_strategy.signals import SignalCalculator


def _make_calc(cfg: StrategyConfig) -> SignalCalculator:
    # _turnover_thresholds 只用 self._cfg 与 self._listing_dates，data 传 None 即可
    return SignalCalculator(data=None, config=cfg)  # type: ignore[arg-type]


def test_turnover_profile_ipo_uses_high_thresholds():
    # Arrange：注入近期 IPO 上市日
    cfg = StrategyConfig()
    calc = _make_calc(cfg)
    calc.set_listing_dates({"US.RDDT": date.today()})

    # Act
    warn, danger = calc._turnover_thresholds("US.RDDT")

    # Assert：IPO 走高换手阈值
    assert (warn, danger) == (cfg.turnover_warning, cfg.turnover_danger)


def test_turnover_profile_general_uses_low_thresholds():
    # Arrange：未注入上市日 → 视为成熟股
    cfg = StrategyConfig()
    calc = _make_calc(cfg)

    # Act
    warn, danger = calc._turnover_thresholds("US.AAPL")

    # Assert：成熟股走低换手阈值
    assert (warn, danger) == (
        cfg.general_turnover_warning,
        cfg.general_turnover_danger,
    )
    assert cfg.general_turnover_danger < cfg.turnover_warning


def test_watchlist_from_env(monkeypatch):
    # Arrange
    monkeypatch.setenv("WATCHLIST", " US.AAPL, US.TSLA ,, US.NVDA ")

    # Act
    cfg = StrategyConfig.from_env()

    # Assert：去空白、去空项
    assert cfg.watchlist == ("US.AAPL", "US.TSLA", "US.NVDA")


def test_watchlist_default_empty():
    assert StrategyConfig().watchlist == ()


# ── IPO 列表解析（回归：列名 list_time + 排除未上市预计 IPO）──────────────
def test_fetch_recent_ipos_parses_list_time_column():
    import pandas as pd

    from datetime import timedelta

    from us_strategy.main import _fetch_recent_ipos

    today = date.today()
    df = pd.DataFrame(
        {
            "code": ["US.NEW", "US.OLD", "US.FUTURE"],
            "name": ["New Co", "Old Co", "Future Co"],
            # moomoo 真实列名为 list_time（非 listing/ipo_date）
            "list_time": [
                today.isoformat(),
                (today - timedelta(days=90)).isoformat(),
                (today + timedelta(days=5)).isoformat(),  # 尚未上市的预计 IPO
            ],
        }
    )

    class _FakeQuote:
        def get_ipo_list(self, market):
            return 0, df  # RET_OK

    class _FakeData:
        _quote = _FakeQuote()

    result = _fetch_recent_ipos(_FakeData(), markets=("US",), days=10, today=today)

    # 只保留近 10 天内"已上市"的 US.NEW；排除过期 US.OLD 与未上市 US.FUTURE
    assert result == {"US.NEW": today}


def test_fetch_today_ipos_only_includes_same_day_listings():
    import pandas as pd

    from datetime import timedelta

    from us_strategy.main import _fetch_today_ipos

    today = date.today()
    df = pd.DataFrame(
        {
            "code": ["US.TODAY", "US.OLD", "US.FUTURE"],
            "name": ["Today Co", "Old Co", "Future Co"],
            "list_time": [
                today.isoformat(),
                (today - timedelta(days=1)).isoformat(),
                (today + timedelta(days=1)).isoformat(),
            ],
            "ipo_price_min": [10.0, 20.0, 30.0],
            "ipo_price_max": [12.0, 22.0, 32.0],
            "issue_size": [1_000_000, 2_000_000, 3_000_000],
        }
    )

    class _FakeQuote:
        def get_ipo_list(self, market):
            return 0, df

    class _FakeData:
        _quote = _FakeQuote()

    result, errors = _fetch_today_ipos(_FakeData(), markets=("US",), today=today)

    assert errors == []
    assert list(result) == ["US.TODAY"]
    assert result["US.TODAY"].name == "Today Co"
    assert result["US.TODAY"].ipo_price_min == "10.0"
