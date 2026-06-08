# -*- coding: utf-8 -*-
"""深度核查后新增能力的纯逻辑单测（港股，不连接 OpenD）。

覆盖：option_iv 纳入加权、回测无风险利率、IPO origin 生命周期降级、
walk-forward 去重叠、OBI 距离衰减聚合、order_flow 盘中时效门。
"""

import dataclasses
from datetime import date, timedelta

import moomoo as ft
import pandas as pd

from hk_strategy.backtest import BacktestEngine, BacktestResult
from hk_strategy.clock import market_date
from hk_strategy.config import StrategyConfig
from hk_strategy.signals import SignalCalculator
from hk_strategy.strategy import IPOStrategy


# ── option_iv 纳入 active_weights ──────────────────────────────────────────
def test_option_iv_enters_active_weights_when_enabled() -> None:
    off = StrategyConfig()
    assert "option_iv" not in off.active_weights()

    on = dataclasses.replace(StrategyConfig(), use_option_iv=True, w_option_iv=0.3)
    assert on.active_weights()["option_iv"] == 0.3


# ── 回测无风险利率 ─────────────────────────────────────────────────────────
def _curve_result(rf: float) -> BacktestResult:
    curve = [100.0, 101.0, 100.0, 102.0, 101.0, 103.0]
    return BacktestResult(
        initial_cash=100.0,
        final_value=curve[-1],
        equity_curve=curve,
        risk_free_rate=rf,
    )


def test_default_risk_free_rate_is_realistic_for_hk() -> None:
    # 港股 HIBOR 经联系汇率跟随美元，默认≈3.5%。
    assert StrategyConfig().annual_risk_free_rate == 0.035


def test_risk_free_rate_lowers_sharpe_and_sortino() -> None:
    base = _curve_result(0.0)
    lifted = _curve_result(0.05)
    assert lifted.sharpe < base.sharpe
    assert lifted.sortino < base.sortino


def test_risk_free_rate_zero_is_baseline_invariant() -> None:
    r = _curve_result(0.0)
    assert r._daily_risk_free == 0.0
    assert r.sharpe != 0.0


# ── IPO origin 生命周期降级 ────────────────────────────────────────────────
def _strategy(**overrides) -> IPOStrategy:
    cfg = dataclasses.replace(StrategyConfig(), **overrides)
    return IPOStrategy(calculator=None, config=cfg)  # type: ignore[arg-type]


def test_ipo_origin_downgrades_after_lifecycle() -> None:
    strat = _strategy(ipo_origin_max_hold_days=3)
    old_buy = market_date(strat._cfg.market_timezone) - timedelta(days=30)
    strat.record_buy("HK.OLD", price=10.0, qty=100, buy_date=old_buy, origin="ipo")
    assert strat._is_ipo_code_locked("HK.OLD") is False


def test_ipo_origin_still_locked_within_lifecycle() -> None:
    strat = _strategy(ipo_origin_max_hold_days=30)
    today = market_date(strat._cfg.market_timezone)
    strat.record_buy("HK.NEW", price=10.0, qty=100, buy_date=today, origin="ipo")
    assert strat._is_ipo_code_locked("HK.NEW") is True


def test_ipo_origin_lifecycle_disabled_preserves_legacy() -> None:
    strat = _strategy(ipo_origin_max_hold_days=0)
    old_buy = market_date(strat._cfg.market_timezone) - timedelta(days=365)
    strat.record_buy("HK.OLD", price=10.0, qty=100, buy_date=old_buy, origin="ipo")
    assert strat._is_ipo_code_locked("HK.OLD") is True


# ── walk-forward 去重叠 ────────────────────────────────────────────────────
def test_walk_forward_segments_do_not_share_boundary() -> None:
    engine = BacktestEngine(quote_ctx=None, config=StrategyConfig())  # type: ignore[arg-type]
    calls: list[tuple[str, str]] = []

    def _record(_codes, start, end):
        calls.append((start, end))
        return BacktestResult(initial_cash=100.0, final_value=100.0)

    engine.run = _record  # type: ignore[method-assign]
    engine.run_walk_forward(["HK.X"], "2024-01-01", "2024-12-31", n_splits=3)

    assert len(calls) == 3
    for (_prev_start, prev_end), (cur_start, _cur_end) in zip(calls, calls[1:]):
        assert cur_start > prev_end


# ── OBI 距离衰减聚合 ───────────────────────────────────────────────────────
class _ObiData:
    def get_order_book(self, _code: str, num: int):
        bid = [(10.0 - i * 0.01, 1000 - i * 10, 1, {}) for i in range(num)]
        ask = [(10.0 + i * 0.01, 500 + i * 10, 1, {}) for i in range(num)]
        return ft.RET_OK, {"Bid": bid, "Ask": ask}


def test_obi_uses_distance_decay_weighting() -> None:
    cfg = dataclasses.replace(StrategyConfig(), use_order_book_imbalance=True)
    calc = SignalCalculator(_ObiData(), cfg)  # type: ignore[arg-type]
    out = calc._order_book_imbalance_scores("HK.X")
    assert out is not None
    scores = out["scores"]

    per_level = {
        int(k.split("_l")[1]): v for k, v in scores.items() if k.startswith("obi_l")
    }
    assert len(per_level) >= 2
    decay = {lv: 1.0 / lv for lv in per_level}
    expected = sum(per_level[lv] * decay[lv] for lv in per_level) / sum(decay.values())
    naive_mean = sum(per_level.values()) / len(per_level)

    assert abs(scores["obi"] - expected) < 1e-9
    assert abs(scores["obi"] - naive_mean) > 1e-9


# ── order_flow 盘中时效门 ──────────────────────────────────────────────────
class _StaleTickData:
    def __init__(self, day: date) -> None:
        self._day = day

    def get_rt_ticker(self, _code: str, _num: int):
        return ft.RET_OK, pd.DataFrame(
            [
                {
                    "time": f"{self._day.isoformat()} 00:00:01",
                    "ticker_direction": "BUY",
                    "volume": 1000,
                }
            ]
        )


def test_order_flow_staleness_gate_rejects_old_tick() -> None:
    cfg = dataclasses.replace(StrategyConfig(), order_flow_max_staleness_seconds=60.0)
    day = market_date(cfg.market_timezone)
    calc = SignalCalculator(_StaleTickData(day), cfg)  # type: ignore[arg-type]
    assert calc._order_flow_imbalance("HK.X") is None


def test_order_flow_staleness_gate_disabled_keeps_tick() -> None:
    cfg = dataclasses.replace(StrategyConfig(), order_flow_max_staleness_seconds=0.0)
    day = market_date(cfg.market_timezone)
    calc = SignalCalculator(_StaleTickData(day), cfg)  # type: ignore[arg-type]
    assert calc._order_flow_imbalance("HK.X") == (1000.0, 0.0)
