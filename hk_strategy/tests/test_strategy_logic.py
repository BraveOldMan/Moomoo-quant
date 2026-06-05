# -*- coding: utf-8 -*-
"""strategy 状态机与风控纯逻辑单测（不触发联网评估）。"""

import dataclasses
from datetime import date

from hk_strategy.config import StrategyConfig
from hk_strategy.strategy import IPOStrategy, _trading_days_between


def _strategy(**overrides) -> IPOStrategy:
    cfg = dataclasses.replace(StrategyConfig(), **overrides)
    # calculator 传 None：本测试只调用不触发 evaluate 的状态方法
    return IPOStrategy(calculator=None, config=cfg)  # type: ignore[arg-type]


# ── 加权平均成本 ────────────────────────────────────────────────────────
def test_weighted_average_cost():
    s = _strategy()
    s.record_buy("US.X", price=10.0, qty=100)
    s.record_buy("US.X", price=20.0, qty=100)
    assert s.get_avg_cost("US.X") == 15.0
    assert s.get_qty("US.X") == 200


def test_buy_date_keeps_first():
    s = _strategy()
    s.record_buy("US.X", 10.0, 100)
    first = s._buy_dates["US.X"]
    s.record_buy("US.X", 11.0, 100)
    assert s._buy_dates["US.X"] == first


def test_clear_position_removes_all_state():
    s = _strategy()
    s.record_buy("US.X", 10.0, 100)
    s.clear_position("US.X")
    assert not s.has_position("US.X")
    assert s.get_avg_cost("US.X") == 0.0
    assert s.get_qty("US.X") == 0


def test_restore_position_rebuilds_cost():
    s = _strategy()
    s.restore_position(
        "US.X",
        avg_cost=12.0,
        qty=50,
        buy_date=date(2024, 1, 2),
        tranches_bought=1,
        peak_price=15.0,
    )
    assert s.get_avg_cost("US.X") == 12.0
    assert s.get_qty("US.X") == 50
    assert s.get_peak_price("US.X") == 15.0


# ── 组合熔断 ────────────────────────────────────────────────────────────
def test_circuit_breaker_triggers_on_loss():
    s = _strategy(circuit_breaker_baseline="first_seen", daily_loss_limit_pct=0.02)
    assert s.check_and_update_circuit_breaker(100_000) is False  # 首次设基准
    assert s.check_and_update_circuit_breaker(97_000) is True  # 亏 3% 触发
    assert s._circuit_breaker_active is True


def test_circuit_breaker_no_trigger_within_limit():
    s = _strategy(circuit_breaker_baseline="first_seen", daily_loss_limit_pct=0.05)
    s.check_and_update_circuit_breaker(100_000)
    assert s.check_and_update_circuit_breaker(98_000) is False  # 仅亏 2% < 5%


def test_circuit_breaker_uses_injected_baseline():
    s = _strategy(circuit_breaker_baseline="prev_close", daily_loss_limit_pct=0.02)
    s.set_daily_baseline(100_000)
    s.check_and_update_circuit_breaker(99_000)  # 当前值无关，基准用注入值
    assert s._daily_start_value == 100_000


# ── PDT 交易日计算 ──────────────────────────────────────────────────────
def test_trading_days_between_skips_weekend():
    # 2024-01-05 周五 → 2024-01-08 周一 = 1 个交易日
    assert _trading_days_between(date(2024, 1, 5), date(2024, 1, 8)) == 1


def test_trading_days_between_same_day_zero():
    assert _trading_days_between(date(2024, 1, 8), date(2024, 1, 8)) == 0


def test_trading_days_between_skips_holiday():
    # 2025-04-30 周三 → 2025-05-02 周五，跨劳动节(5/1 休市) = 1 个交易日
    assert _trading_days_between(date(2025, 4, 30), date(2025, 5, 2)) == 1


def test_can_sell_respects_min_hold_days_disabled():
    s = _strategy(min_hold_days=0)
    s.record_buy("US.X", 10.0, 100)
    assert s._can_sell("US.X") is True
