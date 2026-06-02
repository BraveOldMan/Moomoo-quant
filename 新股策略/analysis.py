# -*- coding: utf-8 -*-
"""因子有效性分析：IC/IR、分层回测、锁定期事件研究。

目的：用数据校准 features 中的因子权重，取代"拍脑袋"权重。

纯统计函数（无 scipy 依赖）可独立单测；FactorAnalyzer 需 OpenQuoteContext 取数。

IC 解读：本项目因子均为"风险分"（高=看空），故**有效因子的 IC 应显著为负**
（风险分越高，未来收益越低）。|IC|>0.03、|IR|>0.5 通常视为有意义。
"""

import logging
from dataclasses import dataclass
from datetime import date, timedelta

import moomoo as ft
import pandas as pd

from . import features
from .config import StrategyConfig

logger = logging.getLogger(__name__)


# ── 纯统计 ──────────────────────────────────────────────────────────────
def _pearson(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2 or len(ys) != n:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 0 or vy <= 0:
        return 0.0
    return cov / (vx**0.5 * vy**0.5)


def _rank(values: list[float]) -> list[float]:
    """平均秩（处理并列）。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def information_coefficient(
    factor: list[float], forward_returns: list[float], method: str = "spearman"
) -> float:
    """因子值与未来收益的相关系数。spearman=秩相关（更稳健）。"""
    if len(factor) < 2:
        return 0.0
    if method == "spearman":
        return _pearson(_rank(factor), _rank(forward_returns))
    return _pearson(factor, forward_returns)


@dataclass
class ICSummary:
    n: int
    ic: float
    interpretation: str


def summarize_ic(ic: float, n: int) -> ICSummary:
    if n < 20:
        note = "样本不足，结论不可靠"
    elif ic <= -0.05:
        note = "显著负相关：有效（风险分高→收益低）"
    elif ic <= -0.02:
        note = "弱负相关：边际有效"
    elif ic >= 0.02:
        note = "正相关：方向相反，建议反向或剔除"
    else:
        note = "近似无关：无预测力"
    return ICSummary(n=n, ic=ic, interpretation=note)


def quantile_returns(
    factor: list[float], forward_returns: list[float], n_quantiles: int = 5
) -> dict[int, float]:
    """按因子值分位分组，返回每组平均未来收益。单调性体现因子有效性。"""
    n = len(factor)
    if n < n_quantiles or len(forward_returns) != n:
        return {}
    order = sorted(range(n), key=lambda i: factor[i])
    buckets: dict[int, list[float]] = {q: [] for q in range(n_quantiles)}
    for rank_pos, idx in enumerate(order):
        q = min(n_quantiles - 1, rank_pos * n_quantiles // n)
        buckets[q].append(forward_returns[idx])
    return {q: sum(v) / len(v) for q, v in buckets.items() if v}


# ── 取数 + 分析 ─────────────────────────────────────────────────────────
class FactorAnalyzer:
    def __init__(
        self,
        quote_ctx: ft.OpenQuoteContext,
        config: StrategyConfig,
        horizon: int = 5,
    ):
        self._ctx = quote_ctx
        self._cfg = config
        self._horizon = horizon

    def _fetch(self, code: str, start: str, end: str) -> pd.DataFrame:
        ret, kl, _ = self._ctx.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_DAY, max_count=1000
        )
        if ret != ft.RET_OK or kl.empty:
            return pd.DataFrame()
        ret2, cf = self._ctx.get_capital_flow(
            code, period_type=ft.PeriodType.DAILY, start=start, end=end
        )
        if ret2 == ft.RET_OK and not cf.empty:
            cf = cf.rename(columns={"capital_flow_item_time": "time_key"})
            keep = [c for c in ("time_key", "main_in_flow") if c in cf.columns]
            kl = kl.merge(cf[keep], on="time_key", how="left")
        return kl

    def build_panel(
        self, codes: list[str], start: str, end: str
    ) -> dict[str, list[float]]:
        """构建因子面板：{factor_name: [...], "forward_return": [...]} 对齐。"""
        cfg = self._cfg
        h = self._horizon
        panel: dict[str, list[float]] = {
            "turnover": [],
            "capital": [],
            "momentum": [],
            "forward_return": [],
        }
        for code in codes:
            df = self._fetch(code, start, end)
            if df.empty or len(df) < h + 2:
                continue
            closes = [float(x) for x in df["close"]]
            for i in range(len(df) - h):
                fwd = (closes[i + h] - closes[i]) / closes[i] if closes[i] > 0 else 0.0
                row = df.iloc[i]
                t_rate = float(row.get("turnover_rate") or 0)
                t_usd = float(row.get("turnover") or 0)
                flow = float(row.get("main_in_flow") or 0)
                panel["turnover"].append(
                    features.turnover_score(
                        t_rate, cfg.turnover_warning, cfg.turnover_danger
                    )
                )
                panel["capital"].append(features.capital_flow_score(flow, t_usd))
                bars = min(cfg.momentum_bars, i + 1)
                if bars >= 2 and closes[i - bars + 1] > 0:
                    chg = (closes[i] - closes[i - bars + 1]) / closes[i - bars + 1]
                    panel["momentum"].append(features.momentum_score(chg))
                else:
                    panel["momentum"].append(50.0)
                panel["forward_return"].append(fwd)
        return panel

    def factor_ic(self, codes: list[str], start: str, end: str) -> dict[str, ICSummary]:
        panel = self.build_panel(codes, start, end)
        fwd = panel.get("forward_return", [])
        out: dict[str, ICSummary] = {}
        for name in ("turnover", "capital", "momentum"):
            vals = panel.get(name, [])
            if not vals:
                continue
            ic = information_coefficient(vals, fwd, method="spearman")
            out[name] = summarize_ic(ic, len(vals))
        return out

    def factor_quantiles(
        self, codes: list[str], start: str, end: str, n_quantiles: int = 5
    ) -> dict[str, dict[int, float]]:
        panel = self.build_panel(codes, start, end)
        fwd = panel.get("forward_return", [])
        return {
            name: quantile_returns(panel[name], fwd, n_quantiles)
            for name in ("turnover", "capital", "momentum")
            if panel.get(name)
        }

    def lockup_event_study(
        self,
        listing_dates: dict[str, date],
        pre_days: int = 10,
        post_days: int = 10,
    ) -> dict[str, list[float]]:
        """锁定期到期事件研究：返回每只股票到期日前后窗口的累计异常收益(CAR)。

        异常收益 = 个股日收益 - 基准(SPY)日收益。
        """
        cfg = self._cfg
        results: dict[str, list[float]] = {}
        for code, listing in listing_dates.items():
            expiry = listing + timedelta(days=cfg.lockup_days)
            start = (expiry - timedelta(days=pre_days * 2 + 10)).isoformat()
            end = (expiry + timedelta(days=post_days * 2 + 10)).isoformat()
            stock = self._fetch(code, start, end)
            bench = self._fetch(cfg.backtest_benchmark, start, end)
            if stock.empty or bench.empty:
                continue
            car = self._car(stock, bench, expiry, pre_days, post_days)
            if car:
                results[code] = car
        return results

    def _car(self, stock, bench, expiry, pre, post) -> list[float]:
        s = {str(r["time_key"])[:10]: float(r["close"]) for _, r in stock.iterrows()}
        b = {str(r["time_key"])[:10]: float(r["close"]) for _, r in bench.iterrows()}
        common = sorted(set(s) & set(b))
        if len(common) < 2:
            return []
        # 找到最接近 expiry 的交易日索引
        anchor = min(
            range(len(common)),
            key=lambda i: abs((date.fromisoformat(common[i]) - expiry).days),
        )
        car: list[float] = []
        cum = 0.0
        lo = max(1, anchor - pre)
        hi = min(len(common) - 1, anchor + post)
        for i in range(lo, hi + 1):
            d, prev = common[i], common[i - 1]
            s_ret = (s[d] - s[prev]) / s[prev] if s[prev] > 0 else 0.0
            b_ret = (b[d] - b[prev]) / b[prev] if b[prev] > 0 else 0.0
            cum += s_ret - b_ret
            car.append(cum * 100)  # 百分比累计异常收益
        return car
