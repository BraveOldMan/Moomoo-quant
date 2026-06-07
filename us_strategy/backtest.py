# -*- coding: utf-8 -*-
"""离线回测引擎（与实盘同源评分）。

关键改进 vs 旧版：
  1. 评分与实盘共用 features.score_from_features + 同一套因子权重，杜绝
     "测的不是跑的"。
  2. 成本模型：佣金（每股 + 最低）+ 滑点（基点），买卖均扣。
  3. 基准对比：SPY buy&hold 同期收益。
  4. 风险指标：年化收益、Sharpe、Sortino、Calmar、最大回撤。
  5. walk-forward：按时间切分样本外检验，抑制过拟合。
  6. look-ahead 安全：逐日只用截至当日的历史窗口计算因子。

使用示例：
    engine = BacktestEngine(quote_ctx, cfg, initial_cash=100_000)
    result = engine.run(["US.RDDT", "US.ARM"], start="2024-01-01", end="2024-06-30")
    print(result.report())
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import date
from typing import cast

import moomoo as ft
import pandas as pd

from . import features
from .config import StrategyConfig
from .strategy import _trading_days_between

logger = logging.getLogger(__name__)

_TRADING_DAYS_PER_YEAR = 252


@dataclass
class TradeRecord:
    date: str
    code: str
    side: str  # "BUY" / "SELL"
    price: float
    qty: int
    commission: float = 0.0
    pnl: float = 0.0  # 仅卖出时有效（已扣成本）


@dataclass
class BacktestResult:
    initial_cash: float
    final_value: float
    trades: list[TradeRecord] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)
    benchmark_curve: list[float] = field(default_factory=list)

    @property
    def total_return_pct(self) -> float:
        return (self.final_value - self.initial_cash) / self.initial_cash * 100

    @property
    def benchmark_return_pct(self) -> float:
        if len(self.benchmark_curve) < 2 or self.benchmark_curve[0] <= 0:
            return 0.0
        return (
            (self.benchmark_curve[-1] - self.benchmark_curve[0])
            / self.benchmark_curve[0]
            * 100
        )

    @property
    def alpha_pct(self) -> float:
        return self.total_return_pct - self.benchmark_return_pct

    @property
    def win_rate(self) -> float:
        sells = [t for t in self.trades if t.side == "SELL"]
        if not sells:
            return 0.0
        return sum(1 for t in sells if t.pnl > 0) / len(sells) * 100

    @property
    def total_commission(self) -> float:
        return sum(t.commission for t in self.trades)

    def _daily_returns(self) -> list[float]:
        c = self.equity_curve
        return [(c[i] - c[i - 1]) / c[i - 1] for i in range(1, len(c)) if c[i - 1] > 0]

    @property
    def annualized_return_pct(self) -> float:
        n = len(self.equity_curve)
        if n < 2 or self.initial_cash <= 0 or self.final_value <= 0:
            return 0.0
        years = n / _TRADING_DAYS_PER_YEAR
        if years <= 0:
            return 0.0
        return ((self.final_value / self.initial_cash) ** (1 / years) - 1) * 100

    @property
    def sharpe(self) -> float:
        rets = self._daily_returns()
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        std = math.sqrt(var)
        if std <= 0:
            return 0.0
        return mean / std * math.sqrt(_TRADING_DAYS_PER_YEAR)

    @property
    def sortino(self) -> float:
        rets = self._daily_returns()
        if len(rets) < 2:
            return 0.0
        mean = sum(rets) / len(rets)
        downside = [r for r in rets if r < 0]
        if not downside:
            return 0.0
        dvar = sum(r**2 for r in downside) / len(rets)
        dstd = math.sqrt(dvar)
        if dstd <= 0:
            return 0.0
        return mean / dstd * math.sqrt(_TRADING_DAYS_PER_YEAR)

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for v in self.equity_curve:
            if v > peak:
                peak = v
            if peak > 0:
                dd = (peak - v) / peak
                max_dd = max(max_dd, dd)
        return max_dd * 100

    @property
    def calmar(self) -> float:
        dd = self.max_drawdown_pct
        if dd <= 0:
            return 0.0
        return self.annualized_return_pct / dd

    def report(self) -> str:
        sells = [t for t in self.trades if t.side == "SELL"]
        lines = [
            "=" * 56,
            "回测结果（已扣佣金 + 滑点）",
            "=" * 56,
            f"初始资金:   ${self.initial_cash:>14,.2f}",
            f"最终净值:   ${self.final_value:>14,.2f}",
            f"总收益率:   {self.total_return_pct:>+8.2f}%",
            f"基准收益:   {self.benchmark_return_pct:>+8.2f}%  (SPY buy&hold)",
            f"超额 Alpha: {self.alpha_pct:>+8.2f}%",
            f"年化收益:   {self.annualized_return_pct:>+8.2f}%",
            f"最大回撤:   {self.max_drawdown_pct:>8.2f}%",
            f"Sharpe:     {self.sharpe:>8.2f}",
            f"Sortino:    {self.sortino:>8.2f}",
            f"Calmar:     {self.calmar:>8.2f}",
            f"交易次数:   {len(self.trades)} 笔（买{sum(1 for t in self.trades if t.side == 'BUY')} / 卖{len(sells)}）",
            f"胜率:       {self.win_rate:.1f}%",
            f"总佣金:     ${self.total_commission:,.2f}",
            "=" * 56,
        ]
        return "\n".join(lines)


class BacktestEngine:
    def __init__(
        self,
        quote_ctx: ft.OpenQuoteContext,
        config: StrategyConfig,
        initial_cash: float = 100_000.0,
        listing_dates: dict[str, date] | None = None,
    ):
        self._ctx = quote_ctx
        self._cfg = config
        self._initial_cash = initial_cash
        self._listing_dates = listing_dates or {}

    # ── 数据 ────────────────────────────────────────────────────────────
    def _fetch_one(self, code: str, start: str, end: str) -> pd.DataFrame:
        ret, kl, _ = self._ctx.request_history_kline(
            code, start=start, end=end, ktype=ft.KLType.K_DAY, max_count=1000
        )
        if ret != ft.RET_OK or kl.empty:
            logger.warning("K 线获取失败 %s: %s", code, kl)
            return pd.DataFrame()
        ret2, cf = self._ctx.get_capital_flow(
            code, period_type=ft.PeriodType.DAY, start=start, end=end
        )
        if ret2 == ft.RET_OK and not cf.empty:
            cf = cf.rename(columns={"capital_flow_item_time": "time_key"})
            keep = [c for c in ("time_key", "main_in_flow") if c in cf.columns]
            kl = kl.merge(cf[keep], on="time_key", how="left")
        kl["code"] = code
        return kl

    def _fetch_benchmark(self, start: str, end: str) -> dict[str, float]:
        df = self._fetch_one(self._cfg.backtest_benchmark, start, end)
        if df.empty:
            return {}
        return {str(r["time_key"]): float(r["close"]) for _, r in df.iterrows()}

    def set_listing_dates(self, dates: dict[str, date]) -> None:
        """注入 IPO 上市日，用于回测时选择换手率 profile。"""
        self._listing_dates.update(dates)

    # ── 主回测 ──────────────────────────────────────────────────────────
    def run(self, codes: list[str], start: str, end: str) -> BacktestResult:
        cfg = self._cfg
        frames = [self._fetch_one(c, start, end) for c in codes]
        frames = [f for f in frames if not f.empty]
        if not frames:
            logger.warning("回测数据为空，请确认股票代码和日期范围")
            return BacktestResult(self._initial_cash, self._initial_cash)
        all_data = pd.concat(frames, ignore_index=True)
        bench = self._fetch_benchmark(start, end)
        filter_data = self._fetch_filter_data(start, end)

        trade_dates = sorted(all_data["time_key"].unique())
        weights = cfg.active_weights()

        cash = self._initial_cash
        positions: dict[str, dict] = {}
        trades: list[TradeRecord] = []
        equity_curve: list[float] = []
        benchmark_curve: list[float] = []
        history: dict[str, dict[str, list[float]]] = {}
        bench_closes: list[float] = []

        for dt in trade_dates:
            dt_key = str(dt)
            day_data = all_data[all_data["time_key"] == dt]

            for _, row in day_data.iterrows():
                code = str(row["code"])
                close = float(row["close"])
                high = float(row.get("high") or close)
                low = float(row.get("low") or close)
                turnover_usd = float(row.get("turnover") or 0)
                turnover_rate = float(row.get("turnover_rate") or 0) * 100.0
                main_flow = float(row.get("main_in_flow") or 0)

                h = history.setdefault(
                    code,
                    {
                        "close": [],
                        "high": [],
                        "low": [],
                        "turnover": [],
                        "turnover_rate": [],
                        "main_flow": [],
                    },
                )

                # 信号只能使用上一交易日及以前的数据；当天 close 仅作为执行/估值价。
                if h["close"]:
                    score = self._score(code, h, bench_closes, weights)
                    atr = features.compute_atr(
                        h["high"], h["low"], h["close"], cfg.atr_period
                    )
                    buy_blocks = self._buy_block_reasons(
                        code, dt_key, filter_data
                    )
                    cash, _ = self._apply_decision(
                        dt_key,
                        code,
                        close,
                        score,
                        atr,
                        h["turnover"][-1],
                        buy_blocks,
                        cash,
                        positions,
                        trades,
                    )

                h["close"].append(close)
                h["high"].append(high)
                h["low"].append(low)
                h["turnover"].append(turnover_usd)
                h["turnover_rate"].append(turnover_rate)
                h["main_flow"].append(main_flow)

            equity_curve.append(cash + self._holdings_value(positions, all_data, dt))
            if dt_key in bench:
                bench_closes.append(bench[dt_key])
            if bench_closes:
                benchmark_curve.append(
                    self._initial_cash * bench_closes[-1] / bench_closes[0]
                )

        # 清算剩余持仓
        if trade_dates:
            last_dt = trade_dates[-1]
            for code, pos in list(positions.items()):
                price = _close_at(all_data, last_dt, code)
                if price is not None:
                    proceeds, comm = self._net_proceeds(pos["qty"], price)
                    cash += proceeds
                    pnl = proceeds - pos["total_cost"]
                    trades.append(
                        TradeRecord(
                            str(last_dt), code, "SELL", price, pos["qty"], comm, pnl
                        )
                    )

        return BacktestResult(
            self._initial_cash, cash, trades, equity_curve, benchmark_curve
        )

    def _score(self, code, h, bench_closes, weights) -> float:
        cfg = self._cfg
        turnover_rate = h["turnover_rate"][-1]
        turnover_usd = h["turnover"][-1]
        main_flow = h["main_flow"][-1]
        warn, danger = self._turnover_thresholds(code)
        scores = {
            "turnover": features.turnover_score(turnover_rate, warn, danger),
            "capital": features.capital_flow_score(main_flow, turnover_usd),
        }
        closes = h["close"]
        if len(closes) >= 2:
            bars = min(cfg.momentum_bars, len(closes))
            first, last = closes[-bars], closes[-1]
            if first > 0:
                scores["momentum"] = features.momentum_score((last - first) / first)
        if (
            cfg.use_rs
            and len(closes) > cfg.rs_lookback_days
            and len(bench_closes) > cfg.rs_lookback_days
        ):
            lb = cfg.rs_lookback_days
            s_ret = (closes[-1] - closes[-1 - lb]) / closes[-1 - lb]
            b_ret = (bench_closes[-1] - bench_closes[-1 - lb]) / bench_closes[-1 - lb]
            scores["rs"] = features.rs_score(s_ret, b_ret)
        return features.score_from_features(scores, weights)

    def _apply_decision(
        self,
        dt: str,
        code: str,
        close: float,
        score: float,
        atr: float | None,
        turnover_usd: float,
        buy_block_reasons: list[str],
        cash: float,
        positions: dict[str, dict],
        trades: list[TradeRecord],
    ) -> tuple[float, bool]:
        cfg = self._cfg
        in_pos = code in positions
        tranches = positions[code]["tranches"] if in_pos else 0
        can_exit = in_pos and self._can_exit(positions[code], dt)

        # 浮动止损优先
        if in_pos and cfg.use_trailing_stop:
            pos = positions[code]
            peak = pos.get("peak", pos["avg"])
            if close > peak:
                pos["peak"] = peak = close
            if (
                can_exit
                and peak > 0
                and (peak - close) / peak >= cfg.trailing_stop_pct
            ):
                return self._exit(dt, code, close, cash, positions, trades), True

        # 固定止损
        if in_pos and can_exit:
            avg = positions[code]["avg"]
            if avg > 0 and (avg - close) / avg >= cfg.stop_loss_pct:
                return self._exit(dt, code, close, cash, positions, trades), True

        # 出货
        if in_pos and can_exit and score >= cfg.sell_threshold:
            return self._exit(dt, code, close, cash, positions, trades), True

        # 买入（含加仓）
        can_open = in_pos or len(positions) < cfg.max_positions
        liquidity_ok = turnover_usd >= cfg.min_daily_turnover_usd
        if (
            score < cfg.buy_threshold
            and tranches < cfg.entry_tranches
            and liquidity_ok
            and can_open
            and not buy_block_reasons
        ):
            if cfg.use_atr_sizing and atr and atr > 0:
                net = cash  # 回测以现金近似可用净值
                sized = features.atr_position_size(
                    net, close, atr, cfg.atr_risk_per_trade_pct, cfg.atr_stop_multiple
                )
                qty = sized.qty
            else:
                budget = cash * cfg.position_ratio / cfg.entry_tranches
                qty = int(budget / close) if close > 0 else 0
            cost, comm = self._gross_cost(qty, close)
            if qty > 0 and cash >= cost:
                cash -= cost
                if in_pos:
                    pos = positions[code]
                    pos["qty"] += qty
                    pos["total_cost"] += cost
                    pos["avg"] = pos["total_cost"] / pos["qty"]
                    pos["tranches"] += 1
                else:
                    positions[code] = {
                        "qty": qty,
                        "avg": close,
                        "total_cost": cost,
                        "tranches": 1,
                        "peak": close,
                        "buy_date": _to_date(dt),
                    }
                trades.append(TradeRecord(dt, code, "BUY", close, qty, comm))
        return cash, False

    def _fetch_filter_data(self, start: str, end: str) -> dict[str, pd.DataFrame]:
        """拉取可回测过滤器代理标的日线；仅用于历史买入门禁。"""
        symbols: set[str] = set()
        if self._cfg.use_macro_filter:
            symbols.update(self._cfg.macro_risk_on_symbols)
            symbols.update(self._cfg.macro_risk_off_symbols)
        if self._cfg.use_crypto_filter:
            symbols.update(self._cfg.crypto_filter_symbols)
        data: dict[str, pd.DataFrame] = {}
        for symbol in sorted(symbols):
            df = self._fetch_one(symbol, start, end)
            if not df.empty:
                data[symbol] = df.sort_values("time_key").reset_index(drop=True)
        return data

    def _buy_block_reasons(
        self, code: str, dt: str, filter_data: dict[str, pd.DataFrame]
    ) -> list[str]:
        """按回测日 dt 之前的数据生成买入门禁原因。"""
        reasons: list[str] = []
        cfg = self._cfg
        if cfg.use_macro_filter:
            macro = self._macro_filter_score_at(dt, filter_data)
            if macro is None:
                reasons.append("纳指/VIX宏观过滤数据缺失")
            elif macro >= cfg.macro_filter_block_score:
                reasons.append(f"纳指/VIX宏观风险偏高: score={macro:.1f}")
        if cfg.use_crypto_filter and code in cfg.crypto_filter_codes:
            crypto = self._trend_score_at(
                cfg.crypto_filter_symbols,
                dt,
                filter_data,
                cfg.crypto_filter_lookback_days,
                risk_on=True,
            )
            if crypto is None:
                reasons.append("BTC/ETH过滤数据缺失")
            elif crypto >= cfg.crypto_filter_block_score:
                reasons.append(f"BTC/ETH过滤风险偏高: score={crypto:.1f}")
        return reasons

    def _macro_filter_score_at(
        self, dt: str, filter_data: dict[str, pd.DataFrame]
    ) -> float | None:
        """计算回测日之前的纳指/VIX代理过滤分。"""
        parts: list[float] = []
        risk_on = self._trend_score_at(
            self._cfg.macro_risk_on_symbols,
            dt,
            filter_data,
            self._cfg.macro_filter_lookback_days,
            risk_on=True,
        )
        if risk_on is not None:
            parts.append(risk_on)
        risk_off = self._trend_score_at(
            self._cfg.macro_risk_off_symbols,
            dt,
            filter_data,
            self._cfg.macro_filter_lookback_days,
            risk_on=False,
        )
        if risk_off is not None:
            parts.append(risk_off)
        if not parts:
            return None
        return sum(parts) / len(parts)

    def _trend_score_at(
        self,
        symbols: tuple[str, ...],
        dt: str,
        filter_data: dict[str, pd.DataFrame],
        lookback_days: int,
        risk_on: bool,
    ) -> float | None:
        """按顺序取第一个可用代理，用 dt 之前的日线计算趋势风险分。"""
        for symbol in symbols:
            df = filter_data.get(symbol)
            if df is None or df.empty:
                continue
            hist = df[df["time_key"].astype(str) < dt]
            if len(hist) <= lookback_days:
                continue
            closes = [float(x) for x in hist["close"]]
            first = closes[-1 - lookback_days]
            last = closes[-1]
            if first <= 0:
                continue
            change = (last - first) / first
            if math.isfinite(change):
                return features.asset_trend_score(change, risk_on=risk_on)
        return None

    def _can_exit(self, position: dict, dt: str) -> bool:
        if self._cfg.min_hold_days <= 0:
            return True
        buy_date = position.get("buy_date")
        if not isinstance(buy_date, date):
            return True
        return _trading_days_between(buy_date, _to_date(dt)) >= self._cfg.min_hold_days

    def _turnover_thresholds(self, code: str) -> tuple[float, float]:
        if code in self._listing_dates:
            return self._cfg.turnover_warning, self._cfg.turnover_danger
        return self._cfg.general_turnover_warning, self._cfg.general_turnover_danger

    def _exit(self, dt, code, close, cash, positions, trades) -> float:
        pos = positions.pop(code)
        proceeds, comm = self._net_proceeds(pos["qty"], close)
        pnl = proceeds - pos["total_cost"]
        trades.append(TradeRecord(dt, code, "SELL", close, pos["qty"], comm, pnl))
        return cash + proceeds

    # ── 成本模型 ────────────────────────────────────────────────────────
    def _commission(self, qty: int) -> float:
        return max(self._cfg.commission_min, qty * self._cfg.commission_per_share)

    def _gross_cost(self, qty: int, price: float) -> tuple[float, float]:
        """买入总支出 = 数量×(价×(1+滑点)) + 佣金。"""
        slip = self._cfg.slippage_bps / 10_000.0
        comm = self._commission(qty)
        return qty * price * (1 + slip) + comm, comm

    def _net_proceeds(self, qty: int, price: float) -> tuple[float, float]:
        """卖出净回款 = 数量×(价×(1-滑点)) - 佣金。"""
        slip = self._cfg.slippage_bps / 10_000.0
        comm = self._commission(qty)
        return qty * price * (1 - slip) - comm, comm

    def _holdings_value(self, positions, all_data, dt) -> float:
        total = 0.0
        for c, pos in positions.items():
            price = _close_at(all_data, dt, c)
            if price is not None:
                total += pos["qty"] * price
        return total

    # ── walk-forward ────────────────────────────────────────────────────
    def run_walk_forward(
        self, codes: list[str], start: str, end: str, n_splits: int = 3
    ) -> list[BacktestResult]:
        """将 [start,end] 等分为 n_splits 段分别回测，输出样本外稳健性。"""
        dates = pd.date_range(start=start, end=end, freq="D")
        if len(dates) < n_splits + 1:
            return [self.run(codes, start, end)]
        date_strs: list[str] = dates.strftime("%Y-%m-%d").tolist()
        idx = [int(i * len(date_strs) / n_splits) for i in range(n_splits)]
        idx.append(len(date_strs) - 1)
        bounds = [date_strs[i] for i in idx]
        results = []
        for i in range(n_splits):
            seg_start = bounds[i]
            seg_end = bounds[i + 1]
            logger.info("walk-forward 第 %d 段: %s → %s", i + 1, seg_start, seg_end)
            results.append(self.run(codes, seg_start, seg_end))
        return results


def _close_at(df: pd.DataFrame, dt, code: str) -> float | None:
    rows = df[(df["time_key"] == dt) & (df["code"] == code)]
    if rows.empty:
        return None
    return float(cast("pd.Series", rows["close"]).iloc[0])


def _to_date(value) -> date:
    return date.fromisoformat(str(value)[:10])
