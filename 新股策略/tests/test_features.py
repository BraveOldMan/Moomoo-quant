# -*- coding: utf-8 -*-
"""features 纯函数单测（AAA 结构）。"""

import pytest

from 新股策略 import features as F


# ── 单因子评分边界与单调性 ──────────────────────────────────────────────
def test_turnover_score_low_is_low_risk():
    assert F.turnover_score(0, 80, 150) == pytest.approx(0.0)
    assert 0 < F.turnover_score(40, 80, 150) < 30


def test_turnover_score_monotonic_increasing():
    s1 = F.turnover_score(50, 80, 150)
    s2 = F.turnover_score(100, 80, 150)
    s3 = F.turnover_score(200, 80, 150)
    assert s1 < s2 < s3


def test_turnover_score_clamped_to_100():
    assert F.turnover_score(10_000, 80, 150) == 100.0


def test_capital_outflow_score_high_outflow_high_risk():
    low = F.capital_outflow_score(0.2, 0.55, 0.70)
    high = F.capital_outflow_score(0.9, 0.55, 0.70)
    assert low < high
    assert high == 100.0 or high > 70


def test_momentum_score_uptrend_low_risk():
    assert F.momentum_score(0.2) == pytest.approx(0.0)  # +20% → 0
    assert F.momentum_score(-0.2) == pytest.approx(100.0)  # -20% → 100
    assert F.momentum_score(0.0) == pytest.approx(50.0)


def test_capital_flow_score_inflow_vs_outflow():
    inflow = F.capital_flow_score(main_in_flow=1000, turnover_usd=10000)
    outflow = F.capital_flow_score(main_in_flow=-1000, turnover_usd=10000)
    assert inflow < 50 < outflow


def test_capital_flow_score_zero_turnover_neutral():
    assert F.capital_flow_score(100, 0) == 50.0


def test_orb_score_breakout_directions():
    up = F.orb_score(last=110, orb_high=100, orb_low=90)
    down = F.orb_score(last=80, orb_high=100, orb_low=90)
    inside = F.orb_score(last=95, orb_high=100, orb_low=90)
    assert up < inside < down


def test_orb_score_degenerate_range_neutral():
    assert F.orb_score(100, 100, 100) == 50.0


def test_rs_score_outperform_low_risk():
    assert F.rs_score(0.1, 0.0) < 50
    assert F.rs_score(-0.1, 0.0) > 50


def test_vwap_score_above_below():
    assert F.vwap_score(last=105, vwap=100) < 50
    assert F.vwap_score(last=95, vwap=100) > 50
    assert F.vwap_score(100, 0) == 50.0


# ── 组合评分归一化 ──────────────────────────────────────────────────────
def test_score_from_features_normalizes_missing():
    # 只有两个因子可用，权重应自动归一化
    scores = {"turnover": 0.0, "capital": 100.0}
    weights = {"turnover": 0.25, "capital": 0.55, "momentum": 0.20}
    result = F.score_from_features(scores, weights)
    expected = (0.0 * 0.25 + 100.0 * 0.55) / (0.25 + 0.55)
    assert result == pytest.approx(expected)


def test_score_from_features_empty_returns_neutral():
    assert F.score_from_features({}, {"turnover": 0.5}) == 50.0


def test_score_from_features_zero_weight_excluded():
    scores = {"turnover": 10.0, "orb": 90.0}
    weights = {"turnover": 0.5, "orb": 0.0}
    assert F.score_from_features(scores, weights) == pytest.approx(10.0)


# ── ATR / VWAP / 仓位 ──────────────────────────────────────────────────
def test_compute_atr_basic():
    highs = [10, 11, 12, 13, 14, 15]
    lows = [9, 10, 11, 12, 13, 14]
    closes = [9.5, 10.5, 11.5, 12.5, 13.5, 14.5]
    atr = F.compute_atr(highs, lows, closes, period=3)
    assert atr is not None
    assert atr > 0


def test_compute_atr_insufficient_data():
    assert F.compute_atr([1, 2], [1, 2], [1, 2], period=14) is None


def test_compute_vwap():
    vwap = F.compute_vwap([10, 10], [10, 10], [10, 10], [100, 100])
    assert vwap == pytest.approx(10.0)


def test_compute_vwap_zero_volume():
    assert F.compute_vwap([10], [10], [10], [0]) is None


def test_atr_position_size_risk_budget():
    # 净值 100k，单笔风险 1% = 1000；止损距离 = ATR(2)*2 = 4 → qty=250
    sized = F.atr_position_size(
        net_value=100_000,
        price=50,
        atr=2.0,
        risk_per_trade_pct=0.01,
        stop_multiple=2.0,
        lot_size=1,
    )
    assert sized.qty == 250
    assert sized.stop_distance == pytest.approx(4.0)


def test_atr_position_size_invalid_inputs():
    assert F.atr_position_size(0, 50, 2, 0.01, 2).qty == 0
    assert F.atr_position_size(100_000, 0, 2, 0.01, 2).qty == 0
    assert F.atr_position_size(100_000, 50, 0, 0.01, 2).qty == 0
