# -*- coding: utf-8 -*-
"""回测执行逻辑回归测试。"""

import pandas as pd
import moomoo as ft

from hk_strategy.backtest import BacktestEngine
from hk_strategy.config import StrategyConfig


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
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        short_volume: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self._data = data
        self._short_volume = short_volume or {}

    def request_history_kline(self, code: str, **_kwargs):
        return ft.RET_OK, self._data.get(code, pd.DataFrame()).copy(), None

    def get_capital_flow(self, *_args, **_kwargs):
        return ft.RET_OK, pd.DataFrame()

    def get_daily_short_volume(self, code: str, **_kwargs):
        return ft.RET_OK, self._short_volume.get(code, pd.DataFrame()).copy()


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


def test_backtest_max_positions_zero_allows_unlimited_positions() -> None:
    quote = _Quote(
        {
            "HK.A": _bars("HK.A"),
            "HK.B": _bars("HK.B"),
            "HK.800000": _bars("HK.800000"),
        }
    )
    cfg = StrategyConfig(
        entry_tranches=1, max_positions=0, min_daily_turnover=1_000_000
    )
    result = BacktestEngine(quote, cfg).run(
        ["HK.A", "HK.B"], "2024-01-02", "2024-01-04"
    )

    buy_codes = {trade.code for trade in result.trades if trade.side == "BUY"}

    assert buy_codes == {"HK.A", "HK.B"}


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


def test_backtest_hk_futures_filter_blocks_new_buys() -> None:
    quote = _Quote(
        {
            "HK.TEST": _bars("HK.TEST", closes=[10.0, 10.2, 10.4, 10.6]),
            "HK.HSImain": _bars("HK.HSImain", closes=[100.0, 95.0, 90.0, 85.0]),
            "HK.800000": _bars("HK.800000"),
        }
    )
    cfg = StrategyConfig(
        entry_tranches=1,
        use_hk_futures_filter=True,
        hk_futures_symbols=("HK.HSImain",),
        hk_futures_proxy_symbols=(),
        hk_futures_filter_lookback_days=1,
        hk_futures_filter_block_score=60.0,
        min_daily_turnover=1_000_000,
    )
    result = BacktestEngine(quote, cfg).run(
        ["HK.TEST"], "2024-01-02", "2024-01-05"
    )

    assert not [trade for trade in result.trades if trade.side == "BUY"]


def test_backtest_short_factor_uses_prior_day_short_volume() -> None:
    quote = _Quote(
        {
            "HK.TEST": _bars("HK.TEST"),
            "HK.800000": _bars("HK.800000"),
        },
        short_volume={
            "HK.TEST": pd.DataFrame(
                {
                    "timestamp_str": ["2024-01-02", "2024-01-03"],
                    "short_percent": [60.0, 60.0],
                }
            )
        },
    )
    cfg = StrategyConfig(
        use_short_metrics=True,
        w_turnover=0.0,
        w_capital=0.0,
        w_momentum=0.0,
        w_short=1.0,
        entry_tranches=1,
        min_daily_turnover=1_000_000,
        buy_threshold=50.0,
    )

    result = BacktestEngine(quote, cfg).run(
        ["HK.TEST"], "2024-01-02", "2024-01-04"
    )

    assert not [trade for trade in result.trades if trade.side == "BUY"]


def test_backtest_report_uses_hk_benchmark_label() -> None:
    quote = _Quote({"HK.TEST": _bars("HK.TEST"), "HK.800000": _bars("HK.800000")})
    result = BacktestEngine(quote, StrategyConfig()).run(
        ["HK.TEST"], "2024-01-02", "2024-01-04"
    )

    assert "HK.800000 buy&hold" in result.report()
    assert "SPY buy&hold" not in result.report()
