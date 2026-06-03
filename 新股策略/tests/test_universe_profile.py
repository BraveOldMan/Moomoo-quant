# -*- coding: utf-8 -*-
"""通用化 universe 与换手率 profile 单测（无需 OpenD）。"""

from datetime import date

from 新股策略.config import StrategyConfig
from 新股策略.signals import SignalCalculator


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
