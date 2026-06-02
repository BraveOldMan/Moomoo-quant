# -*- coding: utf-8 -*-
"""统一特征与纯函数评分。

设计目标：**实盘与回测共用同一套评分逻辑**，杜绝"测的不是跑的"。

约定：所有 *_score 函数返回 0–100 的风险分——
    0   = 低风险 / 偏多
    100 = 高风险 / 偏空
特征提取（联网取数）由 signals.py / backtest.py 负责，本模块只做纯计算，
因此全部函数可独立单测，无需 OpenD。
"""

from __future__ import annotations

from dataclasses import dataclass


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, x))


# ── 单因子评分（纯函数）────────────────────────────────────────────────
def turnover_score(rate: float, warning: float, danger: float) -> float:
    """换手率风险分：过低=流动性差，过高=情绪过热出货风险。"""
    if rate < warning:
        return rate / warning * 30.0 if warning > 0 else 0.0
    if rate < danger:
        span = danger - warning
        return 30.0 + (rate - warning) / span * 40.0 if span > 0 else 70.0
    return _clamp(70.0 + (rate - danger) * 0.5)


def capital_outflow_score(out_ratio: float, warning: float, danger: float) -> float:
    """机构资金净流出占比风险分：流出占比越高风险越高。"""
    if out_ratio < warning:
        return out_ratio / warning * 30.0 if warning > 0 else 0.0
    if out_ratio < danger:
        span = danger - warning
        return 30.0 + (out_ratio - warning) / span * 40.0 if span > 0 else 70.0
    return _clamp(70.0 + (out_ratio - danger) * 200.0)


def momentum_score(change_pct: float) -> float:
    """N 日价格动量风险分：+20%→0（强势），-20%→100（弱势）。"""
    return _clamp(50.0 - change_pct * 250.0)


def capital_flow_score(main_in_flow: float, turnover_usd: float) -> float:
    """资金流强度风险分（实盘资金分布不可用时的兜底，回测同源使用）。

    main_in_flow>0 净流入→低风险；净流出→高风险。按成交额归一化消除规模差异。
    """
    if turnover_usd <= 0:
        return 50.0
    intensity = main_in_flow / turnover_usd  # 净流入占成交额比例
    return _clamp(50.0 - intensity * 250.0)


def broker_score(ask_ratio: float) -> float:
    """卖方经纪队列占比（0–1）→ 0–100 风险分。"""
    return _clamp(ask_ratio * 100.0)


def orb_score(last: float, orb_high: float, orb_low: float) -> float:
    """开盘区间突破：上破→低风险，下破→高风险，区间内按位置线性。

    新股首日无历史均线，ORB 比移动均线更适用。
    """
    if orb_high <= orb_low:
        return 50.0
    span = orb_high - orb_low
    if last >= orb_high:
        # 上破幅度越大风险越低，封顶 -30 分
        return _clamp(30.0 - (last - orb_high) / span * 30.0)
    if last <= orb_low:
        return _clamp(70.0 + (orb_low - last) / span * 30.0)
    # 区间内：靠上沿偏多（低分），靠下沿偏空（高分）
    pos = (last - orb_low) / span  # 0..1
    return _clamp(70.0 - pos * 40.0)


def rs_score(stock_return: float, bench_return: float) -> float:
    """相对强弱：跑赢基准→低风险，跑输→高风险。"""
    diff = stock_return - bench_return
    return _clamp(50.0 - diff * 250.0)


def vwap_score(last: float, vwap: float) -> float:
    """VWAP 偏离：价在 VWAP 之上→多头掌控（低风险），之下→弱势（高风险）。"""
    if vwap <= 0:
        return 50.0
    dev = (last - vwap) / vwap
    return _clamp(50.0 - dev * 500.0)


# ── 组合评分 ────────────────────────────────────────────────────────────
def score_from_features(scores: dict[str, float], weights: dict[str, float]) -> float:
    """按"数据可用且权重>0"的因子做归一化加权。

    缺失因子（不在 scores 中）自动剔除并重新归一化，避免某因子在美股
    取不到数据时整体评分被零权重污染。
    """
    usable = {k: weights[k] for k in scores if k in weights and weights[k] > 0}
    total_w = sum(usable.values())
    if total_w <= 0:
        return 50.0
    return sum(scores[k] * w for k, w in usable.items()) / total_w


# ── 技术指标 ────────────────────────────────────────────────────────────
def compute_atr(
    highs: list[float], lows: list[float], closes: list[float], period: int
) -> float | None:
    """Wilder ATR。数据不足返回 None。"""
    n = len(closes)
    if n < period + 1 or len(highs) != n or len(lows) != n:
        return None
    trs: list[float] = []
    for i in range(1, n):
        prev_close = closes[i - 1]
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - prev_close),
            abs(lows[i] - prev_close),
        )
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_vwap(
    highs: list[float], lows: list[float], closes: list[float], volumes: list[float]
) -> float | None:
    """典型价加权的 VWAP（典型价=(H+L+C)/3）。无量返回 None。"""
    if not closes or len(highs) != len(closes) or len(volumes) != len(closes):
        return None
    pv = 0.0
    vol = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        typical = (h + l + c) / 3.0
        pv += typical * v
        vol += v
    if vol <= 0:
        return None
    return pv / vol


@dataclass
class SizingResult:
    qty: int
    stop_distance: float  # 每股止损距离（ATR 模式下有效）


def atr_position_size(
    net_value: float,
    price: float,
    atr: float,
    risk_per_trade_pct: float,
    stop_multiple: float,
    lot_size: int = 1,
) -> SizingResult:
    """按单笔风险预算定仓位：qty ≈ (净值×风险比例) / (ATR×止损倍数)。"""
    if price <= 0 or atr <= 0 or net_value <= 0:
        return SizingResult(0, 0.0)
    stop_distance = atr * stop_multiple
    if stop_distance <= 0:
        return SizingResult(0, 0.0)
    risk_budget = net_value * risk_per_trade_pct
    raw_qty = int(risk_budget / stop_distance)
    raw_qty = (raw_qty // lot_size) * lot_size
    return SizingResult(max(0, raw_qty), stop_distance)
