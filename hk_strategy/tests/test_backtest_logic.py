# -*- coding: utf-8 -*-
"""回测执行逻辑回归测试。"""

import pandas as pd
import moomoo as ft

from hk_strategy.backtest import BacktestEngine
from hk_strategy.config import StrategyConfig


def _bars(code: str, turnovers: list[float] | None = None) -> pd.DataFrame:
    turnover_values = turnovers or [2_000_000.0, 2_000_000.0, 2_000_000.0]
    return pd.DataFrame(
        {
            "time_key": ["2024-01-02", "2024-01-03", "2024-01-04"],
            "open": [10.0, 10.5, 11.0],
            "close": [10.0, 10.5, 11.0],
            "high": [10.2, 10.7, 11.2],
            "low": [9.8, 10.3, 10.8],
            "turnover": turnover_values,
            "turnover_rate": [0.0, 0.0, 0.0],
            "code": [code, code, code],
        }
    )


class _Quote:
    def __init__(self, data: dict[str, pd.DataFrame]) -> None:
        self._data = data

    def request_history_kline(self, code: str, **_kwargs):
        return ft.RET_OK, self._data.get(code, pd.DataFrame()).copy(), None

    def get_capital_flow(self, *_args, **_kwargs):
        return ft.RET_OK, pd.DataFrame()


def test_backtest_uses_prior_history_for_entry_signal() -> None:
    quote = _Quote({"HK.TEST": _bars("HK.TEST"), "HK.800000": _bars("HK.800000")})
    cfg = StrategyConfig(entry_tranches=1, min_daily_turnover=1_000_000)
    result = BacktestEngine(quote, cfg).run(["HK.TEST"], "2024-01-02", "2024-01-04")

    buys = [trade for trade in result.trades if trade.side == "BUY"]

    assert buys
    assert str(buys[0].date) == "2024-01-03"


def test_backtest_respects_max_positions() -> None:
    quote = _Quote(
        {
            "HK.A": _bars("HK.A"),
            "HK.B": _bars("HK.B"),
            "HK.800000": _bars("HK.800000"),
        }
    )
    cfg = StrategyConfig(
        entry_tranches=1, max_positions=1, min_daily_turnover=1_000_000
    )
    result = BacktestEngine(quote, cfg).run(
        ["HK.A", "HK.B"], "2024-01-02", "2024-01-04"
    )

    buy_codes = {trade.code for trade in result.trades if trade.side == "BUY"}

    assert len(buy_codes) == 1


def test_backtest_respects_liquidity_filter() -> None:
    quote = _Quote(
        {
            "HK.THIN": _bars("HK.THIN", turnovers=[10_000.0, 10_000.0, 10_000.0]),
            "HK.800000": _bars("HK.800000"),
        }
    )
    cfg = StrategyConfig(entry_tranches=1, min_daily_turnover=1_000_000)
    result = BacktestEngine(quote, cfg).run(["HK.THIN"], "2024-01-02", "2024-01-04")

    assert not [trade for trade in result.trades if trade.side == "BUY"]
