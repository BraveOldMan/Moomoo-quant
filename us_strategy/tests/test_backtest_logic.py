# -*- coding: utf-8 -*-
"""回测执行逻辑回归测试。"""

import pandas as pd
import moomoo as ft

from us_strategy.backtest import BacktestEngine
from us_strategy.config import StrategyConfig


def _bars(
    code: str,
    turnovers: list[float] | None = None,
    closes: list[float] | None = None,
) -> pd.DataFrame:
    close_values = closes or [10.0, 10.5, 11.0]
    turnover_values = turnovers or [2_000_000.0 for _ in close_values]
    dates = pd.date_range("2024-01-02", periods=len(close_values), freq="D")
    return pd.DataFrame(
        {
            "time_key": dates.strftime("%Y-%m-%d").tolist(),
            "open": close_values,
            "close": close_values,
            "high": [x + 0.2 for x in close_values],
            "low": [x - 0.2 for x in close_values],
            "turnover": turnover_values,
            "turnover_rate": [0.0 for _ in close_values],
            "code": [code for _ in close_values],
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
    quote = _Quote({"US.TEST": _bars("US.TEST"), "US.SPY": _bars("US.SPY")})
    cfg = StrategyConfig(entry_tranches=1, min_daily_turnover_usd=1_000_000)
    result = BacktestEngine(quote, cfg).run(["US.TEST"], "2024-01-02", "2024-01-04")

    buys = [trade for trade in result.trades if trade.side == "BUY"]

    assert buys
    assert str(buys[0].date) == "2024-01-03"


def test_backtest_respects_max_positions() -> None:
    quote = _Quote(
        {
            "US.A": _bars("US.A"),
            "US.B": _bars("US.B"),
            "US.SPY": _bars("US.SPY"),
        }
    )
    cfg = StrategyConfig(entry_tranches=1, max_positions=1)
    result = BacktestEngine(quote, cfg).run(["US.A", "US.B"], "2024-01-02", "2024-01-04")

    buy_codes = {trade.code for trade in result.trades if trade.side == "BUY"}

    assert len(buy_codes) == 1


def test_backtest_respects_liquidity_filter() -> None:
    quote = _Quote(
        {
            "US.THIN": _bars("US.THIN", turnovers=[10_000.0, 10_000.0, 10_000.0]),
            "US.SPY": _bars("US.SPY"),
        }
    )
    cfg = StrategyConfig(entry_tranches=1, min_daily_turnover_usd=1_000_000)
    result = BacktestEngine(quote, cfg).run(["US.THIN"], "2024-01-02", "2024-01-04")

    assert not [trade for trade in result.trades if trade.side == "BUY"]


def test_backtest_macro_filter_blocks_new_buys() -> None:
    quote = _Quote(
        {
            "US.TEST": _bars("US.TEST", closes=[10.0, 10.2, 10.4, 10.6]),
            "US.QQQ": _bars("US.QQQ", closes=[100.0, 95.0, 90.0, 85.0]),
            "US.SPY": _bars("US.SPY"),
        }
    )
    cfg = StrategyConfig(
        entry_tranches=1,
        use_macro_filter=True,
        macro_risk_on_symbols=("US.QQQ",),
        macro_risk_off_symbols=(),
        macro_filter_lookback_days=1,
        macro_filter_block_score=60.0,
    )
    result = BacktestEngine(quote, cfg).run(
        ["US.TEST"], "2024-01-02", "2024-01-05"
    )

    assert not [trade for trade in result.trades if trade.side == "BUY"]
