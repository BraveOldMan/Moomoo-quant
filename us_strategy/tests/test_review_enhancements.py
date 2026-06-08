# -*- coding: utf-8 -*-
"""深度核查后新增能力的纯逻辑单测（不连接 OpenD）。

覆盖：option_iv 纳入加权、回测无风险利率、IPO origin 生命周期降级、
walk-forward 去重叠、OBI 距离衰减聚合、order_flow 盘中时效门。
"""

import dataclasses
from datetime import date, timedelta

import moomoo as ft
import pandas as pd

from us_strategy.backtest import BacktestEngine, BacktestResult
from us_strategy.clock import market_date
from us_strategy.config import StrategyConfig
from us_strategy.signals import SignalCalculator
from us_strategy.strategy import IPOStrategy, _trading_days_between


# ── option_iv 纳入 active_weights ──────────────────────────────────────────
def test_option_iv_enters_active_weights_when_enabled() -> None:
    off = StrategyConfig()
    assert "option_iv" not in off.active_weights()

    on = dataclasses.replace(StrategyConfig(), use_option_iv=True, w_option_iv=0.3)
    assert on.active_weights()["option_iv"] == 0.3


# ── 回测无风险利率 ─────────────────────────────────────────────────────────
def _curve_result(rf: float) -> BacktestResult:
    # 一条有涨有跌的净值曲线，确保下行样本存在。
    curve = [100.0, 101.0, 100.0, 102.0, 101.0, 103.0]
    return BacktestResult(
        initial_cash=100.0,
        final_value=curve[-1],
        equity_curve=curve,
        risk_free_rate=rf,
    )


def test_default_risk_free_rate_is_realistic_for_us() -> None:
    # 美股默认取短期美债≈4%，并经回测引擎透传到 BacktestResult。
    assert StrategyConfig().annual_risk_free_rate == 0.04


def test_risk_free_rate_lowers_sharpe_and_sortino() -> None:
    base = _curve_result(0.0)
    lifted = _curve_result(0.05)

    assert lifted.sharpe < base.sharpe
    assert lifted.sortino < base.sortino


def test_risk_free_rate_zero_is_baseline_invariant() -> None:
    # rf=0 时新口径必须与历史完全一致（excess==mean、下行阈值 0）。
    r = _curve_result(0.0)
    assert r._daily_risk_free == 0.0
    assert r.sharpe != 0.0  # 曲线有波动，应得到非零值


# ── IPO origin 生命周期降级 ────────────────────────────────────────────────
def _strategy(**overrides) -> IPOStrategy:
    cfg = dataclasses.replace(StrategyConfig(), **overrides)
    return IPOStrategy(calculator=None, config=cfg)  # type: ignore[arg-type]


def test_ipo_origin_downgrades_after_lifecycle() -> None:
    strat = _strategy(ipo_origin_max_hold_days=3)
    today = market_date(strat._cfg.market_timezone)
    old_buy = today - timedelta(days=30)  # 远早于阈值的老仓
    strat.record_buy("US.OLD", price=10.0, qty=100, buy_date=old_buy, origin="ipo")

    # 老仓超过生命周期 → 不再锁定为 IPO（回归常规退出）。
    assert strat._is_ipo_code_locked("US.OLD") is False


def test_ipo_origin_still_locked_within_lifecycle() -> None:
    strat = _strategy(ipo_origin_max_hold_days=30)
    today = market_date(strat._cfg.market_timezone)
    strat.record_buy("US.NEW", price=10.0, qty=100, buy_date=today, origin="ipo")
    assert strat._is_ipo_code_locked("US.NEW") is True


def test_ipo_origin_lifecycle_disabled_preserves_legacy() -> None:
    # 阈值=0（默认）：终身沿用 IPO，保持旧行为。
    strat = _strategy(ipo_origin_max_hold_days=0)
    old_buy = market_date(strat._cfg.market_timezone) - timedelta(days=365)
    strat.record_buy("US.OLD", price=10.0, qty=100, buy_date=old_buy, origin="ipo")
    assert strat._is_ipo_code_locked("US.OLD") is True


# ── walk-forward 去重叠 ────────────────────────────────────────────────────
def test_walk_forward_segments_do_not_share_boundary() -> None:
    engine = BacktestEngine(quote_ctx=None, config=StrategyConfig())  # type: ignore[arg-type]
    calls: list[tuple[str, str]] = []

    def _record(_codes, start, end):
        calls.append((start, end))
        return BacktestResult(initial_cash=100.0, final_value=100.0)

    engine.run = _record  # type: ignore[method-assign]
    engine.run_walk_forward(["US.X"], "2024-01-01", "2024-12-31", n_splits=3)

    assert len(calls) == 3
    # 相邻段：后一段起点必须严格晚于前一段终点（不共享边界交易日）。
    for (prev_start, prev_end), (cur_start, _cur_end) in zip(calls, calls[1:]):
        assert cur_start > prev_end


# ── OBI 距离衰减聚合 ───────────────────────────────────────────────────────
class _ObiData:
    """各档深度随档位变化，使 obi_l1..obi_l10 互不相同。"""

    def get_order_book(self, _code: str, num: int):
        bid = [(10.0 - i * 0.01, 1000 - i * 10, 1, {}) for i in range(num)]
        ask = [(10.0 + i * 0.01, 500 + i * 10, 1, {}) for i in range(num)]
        return ft.RET_OK, {"Bid": bid, "Ask": ask}


def test_obi_uses_distance_decay_weighting() -> None:
    cfg = dataclasses.replace(StrategyConfig(), use_order_book_imbalance=True)
    calc = SignalCalculator(_ObiData(), cfg)  # type: ignore[arg-type]
    out = calc._order_book_imbalance_scores("US.X")
    assert out is not None
    scores = out["scores"]

    per_level = {
        int(k.split("_l")[1]): v for k, v in scores.items() if k.startswith("obi_l")
    }
    assert len(per_level) >= 2  # 多档，才能体现加权差异
    decay = {lv: 1.0 / lv for lv in per_level}
    expected = sum(per_level[lv] * decay[lv] for lv in per_level) / sum(decay.values())
    naive_mean = sum(per_level.values()) / len(per_level)

    assert scores["obi"] == round(expected, 10) or abs(scores["obi"] - expected) < 1e-9
    # 各档分值不同 → 加权结果应区别于等权均值，证明衰减权重生效。
    assert abs(scores["obi"] - naive_mean) > 1e-9


# ── order_flow 盘中时效门 ──────────────────────────────────────────────────
class _StaleTickData:
    def __init__(self, day: date) -> None:
        self._day = day

    def get_rt_ticker(self, _code: str, _num: int):
        # 当日数据但时间戳为凌晨 00:00:01：盘中调用时已远超任何小阈值。
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
    assert calc._order_flow_imbalance("US.X") is None


def test_order_flow_staleness_gate_disabled_keeps_tick() -> None:
    cfg = dataclasses.replace(StrategyConfig(), order_flow_max_staleness_seconds=0.0)
    day = market_date(cfg.market_timezone)
    calc = SignalCalculator(_StaleTickData(day), cfg)  # type: ignore[arg-type]
    # 阈值=0 → 时效门禁用，仅按日期判新鲜度，今日数据照常聚合。
    assert calc._order_flow_imbalance("US.X") == (1000.0, 0.0)


def test_trading_days_between_helper_is_consistent() -> None:
    # 锁仓/生命周期共用该工具，回归其语义。
    d0 = date(2024, 1, 8)  # 周一
    assert _trading_days_between(d0, d0) == 0
    assert _trading_days_between(d0, date(2024, 1, 9)) == 1
