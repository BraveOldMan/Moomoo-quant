# -*- coding: utf-8 -*-
"""行情字段质量门禁回归测试。"""

import math

import moomoo as ft
import pandas as pd
import pytest

from hk_strategy import features
from hk_strategy.config import StrategyConfig
from hk_strategy.signals import SignalCalculator


def test_score_from_features_ignores_nan_scores() -> None:
    scores = {"turnover": math.nan, "capital": 20.0}
    weights = {"turnover": 0.5, "capital": 0.5}

    assert features.score_from_features(scores, weights) == pytest.approx(20.0)


def test_score_from_features_all_nan_returns_neutral() -> None:
    assert features.score_from_features(
        {"turnover": math.nan}, {"turnover": 1.0}
    ) == pytest.approx(50.0)


def test_signal_calculator_rejects_missing_turnover_rate() -> None:
    class _Data:
        def get_market_snapshot(self, _code: str) -> tuple[int, pd.DataFrame]:
            return ft.RET_OK, pd.DataFrame(
                [{"last_price": 10.0, "turnover": 2_000_000.0}]
            )

        def get_capital_distribution(self, _code: str) -> tuple[int, pd.DataFrame]:
            return ft.RET_ERROR, pd.DataFrame()

        def request_history_kline(self, *_args, **_kwargs):
            return ft.RET_ERROR, pd.DataFrame(), None

    calc = SignalCalculator(_Data(), StrategyConfig())

    assert calc.calculate("US.TEST", last_price=10.0) is None
