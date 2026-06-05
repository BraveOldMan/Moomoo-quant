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

import math
from dataclasses import dataclass


def _clamp(x: float, lo: float = 0.0, hi: float = 100.0) -> float:
    if not math.isfinite(x):
        return (lo + hi) / 2.0
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


# ── 盘中微观结构 ────────────────────────────────────────────────────────
def order_flow_score(buy_vol: float, sell_vol: float) -> float:
    """主动买卖盘失衡（CVD）风险分。

    imbalance =(主动买 − 主动卖)/(主动买 + 主动卖)，∈[-1,1]。
    净主动买入（imbalance>0）→ 多头主导 → 低风险；净卖出→高风险。
    数据来自 get_rt_ticker 的 ticker_direction(BUY/SELL)，是美股可用的
    order-flow 信号，替代港股专用的 broker_queue。
    """
    total = buy_vol + sell_vol
    if total <= 0:
        return 50.0
    imbalance = (buy_vol - sell_vol) / total
    return _clamp(50.0 - imbalance * 50.0)


def order_book_imbalance_score(bid_depth: float, ask_depth: float) -> float:
    """盘口失衡 OBI 风险分。

    OBI =(买盘挂单量 − 卖盘挂单量)/(买盘 + 卖盘)，∈[-1,1]。
    买盘更厚（OBI>0）→ 支撑强 → 低风险；卖盘压制→高风险。
    取自 get_order_book 前 N 档累计挂单量。
    """
    total = bid_depth + ask_depth
    if total <= 0:
        return 50.0
    obi = (bid_depth - ask_depth) / total
    return _clamp(50.0 - obi * 50.0)


def linregress_slope(values: list[float]) -> float | None:
    """对等间隔序列做最小二乘斜率（x=0,1,2,...）。点数不足返回 None。"""
    n = len(values)
    if n < 2:
        return None
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    denom = sum((i - mean_x) ** 2 for i in range(n))
    if denom <= 0:
        return None
    num = sum((i - mean_x) * (values[i] - mean_y) for i in range(n))
    return num / denom


def flow_trend_score(slope: float, turnover_usd: float) -> float:
    """日内机构资金流斜率风险分。

    slope = 累计(super+big)净流入序列的每分钟斜率（美元/分钟）。
    按成交额归一化消除规模差异：吸筹（斜率>0）→低风险，派发→高风险。
    """
    if turnover_usd <= 0:
        return 50.0
    intensity = slope / turnover_usd  # 每分钟净流入占成交额比例
    return _clamp(50.0 - intensity * 25_000.0)


# ── 做空面 ──────────────────────────────────────────────────────────────
def short_volume_score(daily_short_pct: float) -> float:
    """每日卖空占比风险分：当日成交中卖空比例越高，抛压越大→高风险。

    daily_short_pct 为百分数（如 13.6 表示 13.6%）。中枢约 15%（美股常见）。
    """
    return _clamp(daily_short_pct * (50.0 / 15.0))


def short_squeeze_score(short_percent: float, days_to_cover: float) -> float:
    """空头拥挤度风险分（结算 short interest，双月、滞后）。

    方向需 IC 校准：高 short% / 高 days_to_cover 既是潜在抛压、也是逼空燃料。
    此处给温和的"高拥挤=偏高风险"基线映射（short% 10% → 50 中枢），
    校准为负 IC（拥挤反而预示反弹）时可在 config 反向。
    """
    base = _clamp(short_percent * (50.0 / 10.0))
    # days_to_cover 高 → 平仓困难，轻微加权
    adj = _clamp(base + (days_to_cover - 3.0) * 2.0)
    return adj


# ── 期权隐含信息 ────────────────────────────────────────────────────────
def iv_skew_score(put_iv: float, call_iv: float) -> float:
    """IV 偏斜风险分：put_iv − call_iv > 0（put 偏贵）= 下行恐慌→高风险。

    IV 为百分数（如 70.0）。每 10 个点偏斜 ≈ 25 分。
    """
    if put_iv <= 0 or call_iv <= 0:
        return 50.0
    skew = put_iv - call_iv
    return _clamp(50.0 + skew * 2.5)


def pcr_score(put_oi: float, call_oi: float) -> float:
    """Put/Call 持仓量比风险分：PCR 高（看跌持仓多）→高风险。

    PCR=1 → 50 中枢；PCR=2 → 100，PCR=0 → 0。
    """
    total = put_oi + call_oi
    if total <= 0:
        return 50.0
    pcr = put_oi / call_oi if call_oi > 0 else 2.0
    return _clamp(pcr * 50.0)


# ── 组合评分 ────────────────────────────────────────────────────────────
def score_from_features(scores: dict[str, float], weights: dict[str, float]) -> float:
    """按"数据可用且权重>0"的因子做归一化加权。

    缺失因子（不在 scores 中）自动剔除并重新归一化，避免某因子在美股
    取不到数据时整体评分被零权重污染。
    """
    usable = {
        k: weights[k]
        for k in scores
        if (
            k in weights
            and weights[k] > 0
            and math.isfinite(weights[k])
            and math.isfinite(scores[k])
        )
    }
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
    for high, low, close, volume in zip(highs, lows, closes, volumes):
        typical = (high + low + close) / 3.0
        pv += typical * volume
        vol += volume
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
