# -*- coding: utf-8 -*-
"""BacktestResult 风险指标单测。"""

import pytest

from us_strategy.backtest import BacktestResult


def _result(curve, bench=None, final=None):
    return BacktestResult(
        initial_cash=curve[0],
        final_value=final if final is not None else curve[-1],
        equity_curve=curve,
        benchmark_curve=bench or [],
    )


def test_total_return_pct():
    r = _result([100, 110], final=110)
    assert r.total_return_pct == pytest.approx(10.0)


def test_max_drawdown():
    # 100 → 120 → 90：峰值120回撤到90 = 25%
    r = _result([100, 120, 90, 95])
    assert r.max_drawdown_pct == pytest.approx(25.0)


def test_no_drawdown_on_monotonic_curve():
    r = _result([100, 101, 102, 103])
    assert r.max_drawdown_pct == pytest.approx(0.0)


def test_alpha_vs_benchmark():
    r = _result([100, 130], bench=[100, 110], final=130)
    assert r.total_return_pct == pytest.approx(30.0)
    assert r.benchmark_return_pct == pytest.approx(10.0)
    assert r.alpha_pct == pytest.approx(20.0)


def test_sharpe_positive_for_upward_curve():
    r = _result([100, 101, 102, 103, 104, 105])
    assert r.sharpe > 0


def test_win_rate():
    from us_strategy.backtest import TradeRecord

    r = BacktestResult(
        initial_cash=100,
        final_value=120,
        trades=[
            TradeRecord("d", "US.X", "SELL", 10, 1, pnl=5),
            TradeRecord("d", "US.Y", "SELL", 10, 1, pnl=-3),
        ],
    )
    assert r.win_rate == pytest.approx(50.0)


def test_report_runs():
    r = _result([100, 110, 105], bench=[100, 102, 104])
    assert "回测结果" in r.report()
