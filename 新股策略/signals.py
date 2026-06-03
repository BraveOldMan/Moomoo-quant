# -*- coding: utf-8 -*-
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import moomoo as ft

from . import features
from .config import StrategyConfig
from .data_access import DataAccess

logger = logging.getLogger(__name__)


@dataclass
class SignalResult:
    code: str
    scores: dict[str, float]  # 各因子风险分（0–100，高=高风险）
    composite_score: float
    turnover_rate: float
    liquidity_ok: bool
    lockup_warning: bool
    atr: float | None = None
    last_price: float | None = None
    extra: dict = field(default_factory=dict)  # 调试用原始特征

    # 便捷访问（缺失因子返回中性 50）
    @property
    def turnover_score(self) -> float:
        return self.scores.get("turnover", 50.0)

    @property
    def capital_score(self) -> float:
        return self.scores.get("capital", 50.0)

    @property
    def momentum_score(self) -> float:
        return self.scores.get("momentum", 50.0)

    @property
    def broker_score(self) -> float:
        return self.scores.get("broker", -1.0)

    def __str__(self) -> str:
        flags = []
        if not self.liquidity_ok:
            flags.append("LOW_LIQ")
        if self.lockup_warning:
            flags.append("LOCKUP")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        parts = ", ".join(f"{k}={v:.1f}" for k, v in self.scores.items())
        atr_str = f", atr={self.atr:.3f}" if self.atr else ""
        return (
            f"{self.code}: composite={self.composite_score:.1f}"
            f" [{parts}{atr_str}]{flag_str}"
        )


class SignalCalculator:
    """从 DataAccess 提取特征，调用 features 纯函数评分。"""

    def __init__(self, data: DataAccess, config: StrategyConfig, signal_log=None):
        self._data = data
        self._cfg = config
        self._listing_dates: dict[str, date] = {}
        # 可选前向日志 sink（实盘传入 SignalLogStore；回测/分析为 None 不落库）
        self._signal_log = signal_log

    def set_listing_dates(self, dates: dict[str, date]) -> None:
        self._listing_dates.update(dates)

    def calculate(
        self, code: str, last_price: float | None = None
    ) -> SignalResult | None:
        cfg = self._cfg
        ret, snap = self._data.get_market_snapshot(code)
        if ret != ft.RET_OK or snap.empty:
            logger.warning("快照获取失败，跳过 %s", code)
            return None
        row = snap.iloc[0]

        rate = _safe_float(row.get("turnover_rate"))
        turnover_usd = _safe_float(row.get("turnover"))
        if last_price is None:
            last_price = _safe_float(row.get("last_price")) or None
        liquidity_ok = turnover_usd >= cfg.min_daily_turnover_usd

        scores: dict[str, float] = {}
        extra: dict = {}

        # ── 换手率（核心，按标的自动分 IPO/成熟股 profile）──────────
        warn, danger = self._turnover_thresholds(code)
        scores["turnover"] = features.turnover_score(rate, warn, danger)

        # ── 机构资金分布（核心；美股可能不可用，缺失则降级）──────────
        out_ratio = self._capital_out_ratio(code)
        if out_ratio is not None:
            scores["capital"] = features.capital_outflow_score(
                out_ratio, cfg.inst_outflow_warning, cfg.inst_outflow_danger
            )
            extra["inst_out_ratio"] = out_ratio

        # ── 日线衍生：动量 / ATR / RS ───────────────────────────────
        daily = self._daily_kline(code)
        atr_val = None
        if daily is not None and not daily.empty:
            mom = self._momentum_change(daily)
            if mom is not None:
                scores["momentum"] = features.momentum_score(mom)
                extra["momentum_change"] = mom
            atr_val = self._atr(daily)
            if cfg.use_rs:
                rs = self._rs(daily)
                if rs is not None:
                    scores["rs"] = features.rs_score(*rs)
                    extra["rs"] = rs

        # ── 盘中分钟线衍生：ORB / VWAP ──────────────────────────────
        if (cfg.use_orb or cfg.use_vwap_signal) and last_price:
            minute = self._intraday_minute(code)
            if minute is not None and not minute.empty:
                if cfg.use_orb:
                    orb = self._orb_bounds(minute)
                    if orb is not None:
                        scores["orb"] = features.orb_score(last_price, *orb)
                        extra["orb"] = orb
                if cfg.use_vwap_signal:
                    vwap = self._vwap(minute)
                    if vwap is not None:
                        scores["vwap"] = features.vwap_score(last_price, vwap)
                        extra["vwap"] = vwap

        # ── 经纪队列（美股一般不可用，默认关闭）────────────────────
        if cfg.use_broker_signal:
            ask_ratio = self._broker_ask_ratio(code)
            if ask_ratio is not None:
                scores["broker"] = features.broker_score(ask_ratio)

        # ── 盘中微观结构：CVD 主动买卖盘 / 盘口失衡 OBI ────────────────
        if cfg.use_order_flow:
            of = self._order_flow_imbalance(code)
            if of is not None:
                buy_vol, sell_vol = of
                scores["order_flow"] = features.order_flow_score(buy_vol, sell_vol)
                extra["order_flow"] = of
        if cfg.use_order_book_imbalance:
            ob = self._order_book_imbalance(code)
            if ob is not None:
                bid_depth, ask_depth = ob
                scores["obi"] = features.order_book_imbalance_score(
                    bid_depth, ask_depth
                )
                extra["obi"] = ob

        # ── 日内机构资金流斜率 ──────────────────────────────────────
        if cfg.use_intraday_flow:
            slope = self._intraday_flow_slope(code)
            if slope is not None and turnover_usd > 0:
                scores["intraday_flow"] = features.flow_trend_score(slope, turnover_usd)
                extra["intraday_flow_slope"] = slope

        # ── 做空面：空头拥挤度 / 每日卖空比例 ───────────────────────
        if cfg.use_short_metrics:
            short = self._short_score(code)
            if short is not None:
                scores["short"] = short

        # ── 期权隐含：IV skew / Put-Call Ratio ──────────────────────
        if cfg.use_option_iv and last_price:
            opt = self._option_iv_score(code, last_price)
            if opt is not None:
                scores["option_iv"] = opt[0]
                extra["option_iv"] = opt[1]

        # 至少要有换手率或资金分布之一，否则信号不可信
        if "turnover" not in scores and "capital" not in scores:
            logger.warning("核心因子均缺失，跳过 %s", code)
            return None

        composite = features.score_from_features(scores, cfg.active_weights())
        lockup_warning = self._check_lockup_warning(code)

        # 前向日志：记录各因子分 + 当时价格，供 analysis 前向 IC 校准（尤其 CVD/OBI）
        if self._signal_log is not None and last_price:
            try:
                self._signal_log.log(code, last_price, scores)
            except Exception as exc:
                logger.debug("前向日志写入失败 %s: %s", code, exc)

        return SignalResult(
            code=code,
            scores=scores,
            composite_score=composite,
            turnover_rate=rate,
            liquidity_ok=liquidity_ok,
            lockup_warning=lockup_warning,
            atr=atr_val,
            last_price=last_price,
            extra=extra,
        )

    # ── 特征提取 ────────────────────────────────────────────────────────
    def _turnover_thresholds(self, code: str) -> tuple[float, float]:
        """按标的选换手率阈值 profile。

        近期 IPO（在 set_listing_dates 注入的清单中）用 IPO 高换手阈值；
        其余美股（自选/持仓中的成熟股）用成熟股低换手阈值，避免成熟股
        的正常低换手被 IPO 阈值误判为"流动性极低/零风险"。
        """
        cfg = self._cfg
        if code in self._listing_dates:
            return cfg.turnover_warning, cfg.turnover_danger
        return cfg.general_turnover_warning, cfg.general_turnover_danger

    def _capital_out_ratio(self, code: str) -> float | None:
        ret, df = self._data.get_capital_distribution(code)
        if ret != ft.RET_OK or df.empty:
            logger.debug("资金分布不可用 %s", code)
            return None
        r = df.iloc[0]
        try:
            inst_in = _safe_float(r.get("capital_in_super")) + _safe_float(
                r.get("capital_in_big")
            )
            inst_out = _safe_float(r.get("capital_out_super")) + _safe_float(
                r.get("capital_out_big")
            )
        except (TypeError, ValueError):
            return None
        total = inst_in + inst_out
        if total <= 0:
            return None
        return inst_out / total

    def _daily_kline(self, code: str):
        cfg = self._cfg
        bars = max(cfg.momentum_bars, cfg.atr_period + 1, cfg.rs_lookback_days + 1)
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=bars * 3 + 10)).isoformat()
        try:
            ret, df, _ = self._data.request_history_kline(
                code, start=start, end=end, ktype=ft.KLType.K_DAY, max_count=bars
            )
        except Exception as exc:
            logger.warning("日线获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or df.empty:
            return None
        return df

    def _momentum_change(self, daily) -> float | None:
        bars = min(self._cfg.momentum_bars, len(daily))
        if bars < 2:
            return None
        try:
            window = daily["close"].iloc[-bars:]
            first, last = float(window.iloc[0]), float(window.iloc[-1])
        except (KeyError, TypeError, ValueError):
            return None
        if first <= 0:
            return None
        return (last - first) / first

    def _atr(self, daily) -> float | None:
        try:
            highs = [float(x) for x in daily["high"]]
            lows = [float(x) for x in daily["low"]]
            closes = [float(x) for x in daily["close"]]
        except (KeyError, TypeError, ValueError):
            return None
        return features.compute_atr(highs, lows, closes, self._cfg.atr_period)

    def _rs(self, daily) -> tuple[float, float] | None:
        lb = self._cfg.rs_lookback_days
        try:
            closes = [float(x) for x in daily["close"]]
        except (KeyError, TypeError, ValueError):
            return None
        if len(closes) < lb + 1:
            return None
        stock_ret = (closes[-1] - closes[-1 - lb]) / closes[-1 - lb]
        bench = self._daily_kline(self._cfg.rs_benchmark)
        if bench is None or bench.empty:
            return None
        try:
            bcloses = [float(x) for x in bench["close"]]
        except (KeyError, TypeError, ValueError):
            return None
        if len(bcloses) < lb + 1:
            return None
        bench_ret = (bcloses[-1] - bcloses[-1 - lb]) / bcloses[-1 - lb]
        return stock_ret, bench_ret

    def _intraday_minute(self, code: str):
        today = date.today().isoformat()
        try:
            ret, df, _ = self._data.request_history_kline(
                code, start=today, end=today, ktype=ft.KLType.K_1M, max_count=400
            )
        except Exception as exc:
            logger.debug("分钟线获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or df.empty:
            return None
        return df

    def _orb_bounds(self, minute) -> tuple[float, float] | None:
        n = min(self._cfg.orb_minutes, len(minute))
        if n < 1:
            return None
        try:
            head = minute.iloc[:n]
            return float(head["high"].max()), float(head["low"].min())
        except (KeyError, TypeError, ValueError):
            return None

    def _vwap(self, minute) -> float | None:
        try:
            highs = [float(x) for x in minute["high"]]
            lows = [float(x) for x in minute["low"]]
            closes = [float(x) for x in minute["close"]]
            volumes = [float(x) for x in minute["volume"]]
        except (KeyError, TypeError, ValueError):
            return None
        return features.compute_vwap(highs, lows, closes, volumes)

    def _order_flow_imbalance(self, code: str) -> tuple[float, float] | None:
        """逐笔聚合主动买/卖成交量（CVD）。非当日数据视为过期→返回 None。"""
        try:
            ret, df = self._data.get_rt_ticker(code, self._cfg.rt_ticker_num)
        except Exception as exc:
            logger.debug("逐笔获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or df.empty or "ticker_direction" not in df.columns:
            return None
        # 数据新鲜度门控：仅在最新一笔属于今日时有效（盘后会返回上一交易日尾盘）
        try:
            last_time = str(df["time"].iloc[-1])[:10]
            if last_time != date.today().isoformat():
                logger.debug("逐笔数据非当日(%s)，order_flow 跳过 %s", last_time, code)
                return None
        except (KeyError, IndexError):
            return None
        buy_vol = 0.0
        sell_vol = 0.0
        for _, r in df.iterrows():
            direction = str(r.get("ticker_direction"))
            vol = _safe_float(r.get("volume"))
            if direction == "BUY":
                buy_vol += vol
            elif direction == "SELL":
                sell_vol += vol
        if buy_vol + sell_vol <= 0:
            return None
        return buy_vol, sell_vol

    def _order_book_imbalance(self, code: str) -> tuple[float, float] | None:
        """累加盘口前 N 档买/卖挂单量。"""
        try:
            ret, data = self._data.get_order_book(code, self._cfg.obi_levels)
        except Exception as exc:
            logger.debug("盘口获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or not isinstance(data, dict):
            return None
        n = self._cfg.obi_levels
        bid_depth = _sum_book_size(data.get("Bid"), n)
        ask_depth = _sum_book_size(data.get("Ask"), n)
        if bid_depth + ask_depth <= 0:
            return None
        return bid_depth, ask_depth

    def _intraday_flow_slope(self, code: str) -> float | None:
        """日内机构资金流斜率：累计(super+big)净流入序列的每分钟斜率。"""
        try:
            ret, df = self._data.get_capital_flow(code, ft.PeriodType.INTRADAY)
        except Exception as exc:
            logger.debug("日内资金流获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or df.empty:
            return None
        cols = df.columns
        if "super_in_flow" not in cols or "big_in_flow" not in cols:
            return None
        window = min(self._cfg.flow_slope_window, len(df))
        tail = df.iloc[-window:]
        try:
            series = [
                _safe_float(s) + _safe_float(b)
                for s, b in zip(tail["super_in_flow"], tail["big_in_flow"])
            ]
        except (KeyError, TypeError, ValueError):
            return None
        return features.linregress_slope(series)

    def _short_score(self, code: str) -> float | None:
        """综合做空面风险分：优先每日卖空比例（及时），辅以结算空头拥挤度。"""
        cfg = self._cfg
        parts: list[float] = []
        dsv = self._latest_row(lambda: self._data.get_daily_short_volume(code))
        if dsv is not None:
            pct = _safe_float(dsv.get("short_percent"))
            if pct > 0:
                parts.append(features.short_volume_score(pct))
        si = self._latest_row(lambda: self._data.get_short_interest(code))
        if si is not None:
            sp = _safe_float(si.get("short_percent"))
            dtc = _safe_float(si.get("days_to_cover"))
            if sp > 0:
                parts.append(features.short_squeeze_score(sp, dtc))
        if not parts:
            return None
        score = sum(parts) / len(parts)
        # IC 校准为"拥挤预示反弹"时，反向（高拥挤→低风险）
        return 100.0 - score if cfg.short_squeeze_reverse else score

    def _option_iv_score(
        self, code: str, last_price: float
    ) -> tuple[float, dict] | None:
        """IV skew + Put-Call Ratio：取最近到期 ATM call/put 的 IV 与持仓量。"""
        try:
            ret, exp = self._data.get_option_expiration_date(code)
        except Exception as exc:
            logger.debug("期权到期日异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or exp.empty:
            return None
        expiry = self._pick_expiry(exp)
        if expiry is None:
            return None
        try:
            ret2, chain = self._data.get_option_chain(code, expiry, expiry)
        except Exception as exc:
            logger.debug("期权链异常 %s: %s", code, exc)
            return None
        if ret2 != ft.RET_OK or chain.empty:
            return None
        atm = self._atm_codes(chain, last_price)
        if atm is None:
            return None
        call_code, put_code = atm
        call = self._option_snapshot(call_code)
        put = self._option_snapshot(put_code)
        if call is None or put is None:
            return None
        call_iv = _safe_float(call.get("option_implied_volatility"))
        put_iv = _safe_float(put.get("option_implied_volatility"))
        call_oi = _safe_float(call.get("option_open_interest"))
        put_oi = _safe_float(put.get("option_open_interest"))
        sub: list[float] = []
        if call_iv > 0 and put_iv > 0:
            sub.append(features.iv_skew_score(put_iv, call_iv))
        if call_oi + put_oi > 0:
            sub.append(features.pcr_score(put_oi, call_oi))
        if not sub:
            return None
        info = {
            "expiry": expiry,
            "call_iv": call_iv,
            "put_iv": put_iv,
            "put_oi": put_oi,
            "call_oi": call_oi,
        }
        return sum(sub) / len(sub), info

    def _pick_expiry(self, exp) -> str | None:
        """选最近且在 max_expiry_days 内的到期日。"""
        try:
            rows = exp.sort_values("option_expiry_date_distance")
        except (KeyError, TypeError):
            rows = exp
        for _, r in rows.iterrows():
            dist = _safe_float(r.get("option_expiry_date_distance"))
            if 0 <= dist <= self._cfg.option_iv_max_expiry_days:
                return str(r.get("strike_time"))
        return None

    def _atm_codes(self, chain, last_price: float) -> tuple[str, str] | None:
        """从期权链中挑选最接近现价的同行权价 call/put 代码。"""
        try:
            calls = {
                _safe_float(r["strike_price"]): str(r["code"])
                for _, r in chain.iterrows()
                if str(r.get("option_type")) == "CALL"
            }
            puts = {
                _safe_float(r["strike_price"]): str(r["code"])
                for _, r in chain.iterrows()
                if str(r.get("option_type")) == "PUT"
            }
        except (KeyError, TypeError, ValueError):
            return None
        common = [k for k in calls if k in puts and k > 0]
        if not common:
            return None
        strike = min(common, key=lambda k: abs(k - last_price))
        return calls[strike], puts[strike]

    def _option_snapshot(self, opt_code: str) -> dict | None:
        try:
            ret, df = self._data.get_market_snapshot(opt_code)
        except Exception:
            return None
        if ret != ft.RET_OK or df.empty:
            return None
        return df.iloc[0].to_dict()

    def _latest_row(self, fetch) -> dict | None:
        """调用返回 (ret, df[, next_key]) 的接口，取最新一行为 dict。"""
        try:
            result = fetch()
        except Exception as exc:
            logger.debug("做空数据获取异常: %s", exc)
            return None
        if not result or result[0] != ft.RET_OK:
            return None
        df = result[1]
        if df is None or df.empty:
            return None
        return df.iloc[-1].to_dict()

    def _broker_ask_ratio(self, code: str) -> float | None:
        ret, bid_df, ask_df = self._data.get_broker_queue(code)
        if ret != ft.RET_OK:
            return None
        try:
            bid_vol = float(bid_df["order_volume"].sum()) if not bid_df.empty else 0.0
            ask_vol = float(ask_df["order_volume"].sum()) if not ask_df.empty else 0.0
        except (KeyError, TypeError):
            return None
        total = bid_vol + ask_vol
        if total <= 0:
            return None
        return ask_vol / total

    def _check_lockup_warning(self, code: str) -> bool:
        listing = self._listing_dates.get(code)
        if listing is None:
            return False
        cfg = self._cfg
        lockup_expiry = listing + timedelta(days=cfg.lockup_days)
        days_to_expiry = (lockup_expiry - date.today()).days
        return 0 <= days_to_expiry <= cfg.lockup_warning_days


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _sum_book_size(levels, n: int) -> float:
    """累加盘口前 n 档的挂单量。每档形如 (price, size, order_count, {})。"""
    if not levels:
        return 0.0
    total = 0.0
    for lvl in levels[:n]:
        try:
            total += float(lvl[1])
        except (TypeError, ValueError, IndexError):
            continue
    return total
