# -*- coding: utf-8 -*-
"""回测执行逻辑回归测试。"""

import pandas as pd
import moomoo as ft

from us_strategy import features
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
    def __init__(
        self,
        data: dict[str, pd.DataFrame],
        short_data: dict[str, pd.DataFrame] | None = None,
    ) -> None:
        self._data = data
        self._short_data = short_data or {}

    def request_history_kline(self, code: str, **_kwargs):
        return ft.RET_OK, self._data.get(code, pd.DataFrame()).copy(), None

    def get_capital_flow(self, *_args, **_kwargs):
        return ft.RET_OK, pd.DataFrame()

    def get_daily_short_volume(self, code: str, **_kwargs):
        return ft.RET_OK, self._short_data.get(code, pd.DataFrame()).copy()


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
    result = BacktestEngine(quote, cfg).run(
        ["US.A", "US.B"], "2024-01-02", "2024-01-04"
    )

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
    result = BacktestEngine(quote, cfg).run(["US.TEST"], "2024-01-02", "2024-01-05")

    assert not [trade for trade in result.trades if trade.side == "BUY"]


def test_backtest_short_factor_can_block_new_buys() -> None:
    bars = _bars("US.TEST", closes=[10.0, 10.2, 10.4, 10.6])
    short = pd.DataFrame(
        {
            "timestamp_str": bars["time_key"],
            "short_percent": [25.0, 25.0, 25.0, 25.0],
        }
    )
    quote = _Quote(
        {"US.TEST": bars, "US.SPY": _bars("US.SPY", closes=[10.0, 10.1, 10.2, 10.3])},
        {"US.TEST": short},
    )
    cfg = StrategyConfig(
        entry_tranches=1,
        use_short_metrics=True,
        w_turnover=0.0,
        w_capital=0.0,
        w_momentum=0.0,
        w_short=1.0,
        buy_threshold=35.0,
    )

    result = BacktestEngine(quote, cfg).run(
        ["US.TEST"],
        "2024-01-02",
        "2024-01-05",
    )

    assert not [trade for trade in result.trades if trade.side == "BUY"]


def test_backtest_short_factor_allows_low_short_new_buys() -> None:
    bars = _bars("US.TEST", closes=[10.0, 10.2, 10.4, 10.6])
    short = pd.DataFrame(
        {
            "timestamp_str": bars["time_key"],
            "short_percent": [1.0, 1.0, 1.0, 1.0],
        }
    )
    quote = _Quote(
        {"US.TEST": bars, "US.SPY": _bars("US.SPY", closes=[10.0, 10.1, 10.2, 10.3])},
        {"US.TEST": short},
    )
    cfg = StrategyConfig(
        entry_tranches=1,
        use_short_metrics=True,
        w_turnover=0.0,
        w_capital=0.0,
        w_momentum=0.0,
        w_short=1.0,
        buy_threshold=35.0,
    )

    result = BacktestEngine(quote, cfg).run(
        ["US.TEST"],
        "2024-01-02",
        "2024-01-05",
    )

    assert [trade for trade in result.trades if trade.side == "BUY"]


# ── 回测↔主策略一致性（深度核查后新增）────────────────────────────────────


def _score_history(main_flow, closes, short_percent=None):
    n = len(closes)
    return {
        "turnover_rate": [5.0] * n,
        "turnover": [2_000_000.0] * n,
        "main_flow": list(main_flow),
        "close": list(closes),
        "high": list(closes),
        "low": list(closes),
        "short_percent": list(short_percent) if short_percent else [None] * n,
    }


def test_score_drops_capital_without_flow_data() -> None:
    # main_in_flow 缺失（美股默认）→ capital 丢弃，综合分仅 turnover+momentum 归一化，
    # 与实盘"资金分布不可用则丢弃 capital"一致。
    cfg = StrategyConfig()
    engine = BacktestEngine(_Quote({}), cfg)
    weights = cfg.active_weights()
    h = _score_history([None, None], [10.0, 11.0])

    got = engine._score("US.X", h, [], weights)

    warn, danger = engine._turnover_thresholds("US.X")
    expected = features.score_from_features(
        {
            "turnover": features.turnover_score(5.0, warn, danger),
            "momentum": features.momentum_score((11.0 - 10.0) / 10.0),
        },
        weights,
    )
    assert got == expected


def test_score_includes_capital_when_flow_present() -> None:
    # 有真实逐根资金流 → capital 计入（capital_flow_score 代理）。
    cfg = StrategyConfig()
    engine = BacktestEngine(_Quote({}), cfg)
    weights = cfg.active_weights()
    h = _score_history([None, 50_000.0], [10.0, 11.0])

    got = engine._score("US.X", h, [], weights)

    warn, danger = engine._turnover_thresholds("US.X")
    expected = features.score_from_features(
        {
            "turnover": features.turnover_score(5.0, warn, danger),
            "capital": features.capital_flow_score(50_000.0, 2_000_000.0),
            "momentum": features.momentum_score((11.0 - 10.0) / 10.0),
        },
        weights,
    )
    assert got == expected


def test_score_short_matches_live_short_volume_only() -> None:
    # 回测 short == 实盘"仅 daily_short_volume"分支 == features.short_volume_score(pct)。
    cfg = StrategyConfig(use_short_metrics=True, w_short=0.1)
    engine = BacktestEngine(_Quote({}), cfg)
    weights = cfg.active_weights()
    h = _score_history([None, None], [10.0, 10.0], short_percent=[20.0, 25.0])

    got = engine._score("US.X", h, [], weights)

    warn, danger = engine._turnover_thresholds("US.X")
    expected = features.score_from_features(
        {
            "turnover": features.turnover_score(5.0, warn, danger),
            "momentum": features.momentum_score(0.0),
            "short": features.short_volume_score(25.0),
        },
        weights,
    )
    assert got == expected


def test_backtest_circuit_breaker_blocks_new_buys_on_daily_loss() -> None:
    # A 在 day3 暴跌→组合回撤超 daily_loss_limit_pct→当日阻断新开仓；
    # B 因 day2 流动性不足、day3 才可买：熔断开启时 B 被挡，关闭时 B 买入。
    # 决策用上一日数据：X 成交额[低,低,高,高]→前一日成交额到 day4 才达标→X 仅 day4 可买。
    a = _bars("US.A", closes=[10.0, 10.0, 10.0, 2.0])  # day4 暴跌制造组合亏损
    x = _bars(
        "US.X",
        closes=[10.0, 10.0, 10.0, 10.0],
        turnovers=[10_000.0, 10_000.0, 5_000_000.0, 5_000_000.0],
    )
    spy = _bars("US.SPY", closes=[10.0, 10.0, 10.0, 10.0])

    def x_bought(daily_loss_limit_pct: float) -> bool:
        quote = _Quote({"US.A": a.copy(), "US.X": x.copy(), "US.SPY": spy.copy()})
        cfg = StrategyConfig(
            entry_tranches=1,
            max_positions=5,
            min_daily_turnover_usd=1_000_000,
            buy_threshold=35.0,
            daily_loss_limit_pct=daily_loss_limit_pct,
        )
        result = BacktestEngine(quote, cfg).run(
            ["US.A", "US.X"], "2024-01-02", "2024-01-05"
        )
        return any(t.code == "US.X" and t.side == "BUY" for t in result.trades)

    assert x_bought(0.0) is True  # 关闭熔断：X 在 day4 被买入
    assert x_bought(0.001) is False  # 开启熔断：A 暴跌触发熔断，X 被阻断
