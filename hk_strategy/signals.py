# -*- coding: utf-8 -*-
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import moomoo as ft

from dark_pool_proxy import DarkPoolProxyConfig, scan_dark_pool_proxy
from order_book_l2 import (
    L2ImbalanceConfig,
    compute_order_book_metrics,
    evaluate_l2_imbalance,
    metric_levels_for,
)

from . import features
from .clock import market_date, market_datetime
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
    risk_warnings: list[str] = field(default_factory=list)
    buy_block_reasons: list[str] = field(default_factory=list)

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
        if self.risk_warnings:
            flags.append("WARN")
        if self.buy_block_reasons:
            flags.append("BUY_BLOCK")
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
        self._book_depths: dict[str, tuple[float, float]] = {}
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

        rate = _safe_float_or_none(row.get("turnover_rate"))
        turnover_usd = _safe_float_or_none(row.get("turnover"))
        if turnover_usd is None:
            logger.warning("成交额字段缺失或非法，跳过 %s", code)
            return None
        if last_price is None:
            last_price = _safe_float(row.get("last_price")) or None
        liquidity_ok = turnover_usd >= cfg.min_daily_turnover

        scores: dict[str, float] = {}
        extra: dict = {"turnover_usd": turnover_usd}
        risk_warnings: list[str] = []
        buy_block_reasons: list[str] = []

        # ── 换手率阈值（核心，按标的自动分 IPO/成熟股 profile）────────
        warn, danger = self._turnover_thresholds(code)

        # ── 机构资金分布（核心；港股通常可用，缺失则降级）────────────
        out_ratio = self._capital_out_ratio(code)
        if out_ratio is not None:
            scores["capital"] = features.capital_outflow_score(
                out_ratio, cfg.inst_outflow_warning, cfg.inst_outflow_danger
            )
            extra["inst_out_ratio"] = out_ratio

        # ── 日线衍生：动量 / ATR / RS（取输入，打分走共享函数）────────
        daily = self._daily_kline(code)
        atr_val = None
        mom = None
        rs_pair = None
        if daily is not None and not daily.empty:
            mom = self._momentum_change(daily)
            if mom is not None:
                extra["momentum_change"] = mom
            atr_val = self._atr(daily)
            if cfg.use_rs:
                rs_pair = self._rs(daily)
                if rs_pair is not None:
                    extra["rs"] = rs_pair

        # ── 共享 K 线因子（turnover/momentum/rs，与回测同源，杜绝口径漂移）──
        scores.update(
            features.kline_factor_scores(
                turnover_rate=rate,
                turnover_warn=warn,
                turnover_danger=danger,
                momentum_change=mom,
                rs=rs_pair,
            )
        )

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

        # ── 经纪队列（港股可用但须订阅 Broker 数据，默认关闭）────────
        if cfg.use_broker_signal:
            ask_ratio = self._broker_ask_ratio(code)
            if ask_ratio is not None:
                scores["broker"] = features.broker_score(ask_ratio)
                extra["broker"] = {"ask_ratio": ask_ratio}
                if (
                    cfg.use_broker_gate
                    and scores["broker"] >= cfg.microstructure_block_score
                ):
                    buy_block_reasons.append(
                        f"broker queue卖方占优: score={scores['broker']:.1f}"
                    )

        # ── 盘中微观结构：CVD 主动买卖盘 / 盘口失衡 OBI ────────────────
        if cfg.use_hk_status_signal:
            dark_status = row.get("dark_status")
            sec_status = row.get("sec_status")
            scores["hk_status"] = features.hk_status_score(dark_status, sec_status)
            extra["hk_status"] = {
                "dark_status": dark_status,
                "sec_status": sec_status,
            }
            if scores["hk_status"] >= 80.0:
                risk_warnings.append(
                    "HK status risk: "
                    f"dark_status={dark_status} "
                    f"sec_status={sec_status} "
                    f"score={scores['hk_status']:.1f}"
                )

        if cfg.use_order_flow:
            of = self._order_flow_imbalance(code)
            if of is not None:
                buy_vol, sell_vol = of
                scores["order_flow"] = features.order_flow_score(buy_vol, sell_vol)
                total_vol = buy_vol + sell_vol
                extra["order_flow"] = {
                    "buy_vol": buy_vol,
                    "sell_vol": sell_vol,
                    "net_buy_vol": buy_vol - sell_vol,
                    "net_buy_ratio": (
                        (buy_vol - sell_vol) / total_vol if total_vol > 0 else 0.0
                    ),
                }
        if cfg.use_dark_pool_proxy:
            proxy = self._dark_pool_proxy_score(code)
            if proxy is not None:
                scores["dark_pool_proxy"] = proxy[0]
                extra["dark_pool_proxy"] = proxy[1]
        if cfg.use_order_book_imbalance:
            ob = self._order_book_imbalance_scores(code)
            if ob is not None:
                scores.update(ob["scores"])
                extra["obi"] = ob["raw"]
        if cfg.use_order_book_pressure:
            pressure = self._order_book_pressure_score(code)
            if pressure is not None:
                scores["book_pressure"] = pressure[0]
                extra["book_pressure"] = pressure[1]
        if cfg.use_order_book_metrics:
            book_metrics = self._order_book_metric_scores(code)
            if book_metrics is not None:
                scores.update(book_metrics["scores"])
                extra["order_book_metrics"] = book_metrics["raw"]
        if cfg.use_l2_imbalance_tracker:
            l2_signal = self._l2_imbalance_score(code)
            if l2_signal is not None:
                scores["l2_imbalance"] = l2_signal[0]
                extra["l2_imbalance"] = l2_signal[1]

        if cfg.use_microstructure_gate:
            self._append_microstructure_blocks(scores, buy_block_reasons)

        # ── 日内机构资金流斜率 ──────────────────────────────────────
        if cfg.use_intraday_flow:
            slope = self._intraday_flow_slope(code)
            if slope is not None and turnover_usd > 0:
                scores["intraday_flow"] = features.flow_trend_score(slope, turnover_usd)
                extra["intraday_flow_slope"] = slope

        if cfg.use_lunch_continuation:
            lunch = self._lunch_continuation_score(code)
            if lunch is not None:
                scores["lunch_continuation"] = lunch[0]
                extra["lunch_continuation"] = lunch[1]

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
                if opt[0] >= cfg.option_warning_score:
                    risk_warnings.append(f"期权skew/PCR风险偏高: score={opt[0]:.1f}")

        if cfg.use_hk_futures_filter:
            fut = self._hk_futures_filter_score()
            if fut is None:
                buy_block_reasons.append("恒指/国指期货过滤数据缺失")
            else:
                scores["hk_futures_filter"] = fut[0]
                extra["hk_futures_filter"] = fut[1]
                if fut[0] >= cfg.hk_futures_filter_block_score:
                    buy_block_reasons.append(
                        f"恒指/国指期货风险偏高: score={fut[0]:.1f}"
                    )

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
            risk_warnings=risk_warnings,
            buy_block_reasons=buy_block_reasons,
        )

    # ── 特征提取 ────────────────────────────────────────────────────────
    def _turnover_thresholds(self, code: str) -> tuple[float, float]:
        """按标的选换手率阈值 profile。

        近期 IPO（在 set_listing_dates 注入的清单中）用 IPO 高换手阈值；
        其余港股（自选/持仓中的成熟股）用成熟股低换手阈值，避免成熟股
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
        today = market_date(cfg.market_timezone)
        end = today.isoformat()
        start = (today - timedelta(days=bars * 3 + 10)).isoformat()
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
        today = market_date(self._cfg.market_timezone).isoformat()
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
            last_raw = str(df["time"].iloc[-1])
            last_time = last_raw[:10]
            if last_time != market_date(self._cfg.market_timezone).isoformat():
                logger.debug("逐笔数据非当日(%s)，order_flow 跳过 %s", last_time, code)
                return None
        except (KeyError, IndexError):
            return None
        # 盘中时效门：阈值>0 时，最后一笔早于 now−阈值（秒）则判过期跳过。
        max_stale = self._cfg.order_flow_max_staleness_seconds
        if max_stale > 0:
            try:
                last_dt = datetime.strptime(last_raw[:19], "%Y-%m-%d %H:%M:%S")
                now_local = market_datetime(self._cfg.market_timezone).replace(
                    tzinfo=None
                )
                age = (now_local - last_dt).total_seconds()
                if age > max_stale:
                    logger.debug(
                        "逐笔数据过期 %.0fs>%.0fs，order_flow 跳过 %s",
                        age,
                        max_stale,
                        code,
                    )
                    return None
            except ValueError:
                pass
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

    def _dark_pool_proxy_score(self, code: str) -> tuple[float, dict] | None:
        """Score moomoo large-print proxy rows for the current market date."""

        try:
            ret, df = self._data.get_rt_ticker(
                code,
                self._cfg.dark_pool_rt_ticker_num,
            )
        except Exception as exc:
            logger.debug("dark pool proxy ticker fetch failed %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or df.empty:
            return None
        metrics = scan_dark_pool_proxy(
            df,
            config=self._dark_pool_proxy_config(),
            market_date=market_date(self._cfg.market_timezone).isoformat(),
            code=code,
        ).get(code)
        if metrics is None or metrics.print_count <= 0:
            return None
        return metrics.score, metrics.as_dict()

    def _dark_pool_proxy_config(self) -> DarkPoolProxyConfig:
        """Build the shared large-print proxy config from strategy config."""

        return DarkPoolProxyConfig(
            us_min_notional=self._cfg.dark_pool_us_min_notional,
            hk_min_notional=self._cfg.dark_pool_hk_min_notional,
            alert_cooldown_s=self._cfg.dark_pool_alert_cooldown_s,
        )

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

    def _order_book_imbalance_scores(self, code: str) -> dict[str, dict] | None:
        """计算 1/3/5/10 多档 OBI，并用均值作为聚合 obi 分。"""
        buckets = _level_buckets(self._cfg.obi_level_buckets, self._cfg.obi_levels)
        try:
            ret, data = self._data.get_order_book(code, max(buckets))
        except Exception as exc:
            logger.debug("盘口获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or not isinstance(data, dict):
            return None
        scores: dict[str, float] = {}
        raw: dict[str, dict[str, float]] = {}
        for level in buckets:
            bid_depth = _sum_book_size(data.get("Bid"), level)
            ask_depth = _sum_book_size(data.get("Ask"), level)
            if bid_depth + ask_depth <= 0:
                continue
            key = f"obi_l{level}"
            scores[key] = features.order_book_imbalance_score(bid_depth, ask_depth)
            raw[key] = {"bid_depth": bid_depth, "ask_depth": ask_depth}
        if not scores:
            return None
        # 距离衰减加权：档位越深（level 越大）权重越低（1/level），
        # 降低 level-1 盘口跳动噪声对综合 OBI 的影响。
        decay = {k: 1.0 / int(k.split("_l")[1]) for k in scores}
        total_w = sum(decay.values())
        scores["obi"] = sum(scores[k] * decay[k] for k in decay) / total_w
        return {"scores": scores, "raw": raw}

    def _order_book_pressure_score(self, code: str) -> tuple[float, dict] | None:
        """用进程内上一轮盘口深度计算撤单/挂单压力。"""
        level = max(_level_buckets(self._cfg.obi_level_buckets, self._cfg.obi_levels))
        try:
            ret, data = self._data.get_order_book(code, level)
        except Exception as exc:
            logger.debug("盘口压力获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or not isinstance(data, dict):
            return None
        bid_depth = _sum_book_size(data.get("Bid"), level)
        ask_depth = _sum_book_size(data.get("Ask"), level)
        if bid_depth + ask_depth <= 0:
            return None
        previous = self._book_depths.get(code)
        self._book_depths[code] = (bid_depth, ask_depth)
        if previous is None:
            return None
        prev_bid, prev_ask = previous
        score = features.order_book_pressure_score(
            prev_bid, prev_ask, bid_depth, ask_depth
        )
        detail = {
            "level": level,
            "prev_bid_depth": prev_bid,
            "prev_ask_depth": prev_ask,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
        }
        return score, detail

    def _order_book_metric_scores(self, code: str) -> dict[str, dict] | None:
        """Score spread and visible-book slippage from latest L2 snapshot."""

        try:
            ret, data = self._data.get_order_book(code, self._cfg.order_book_levels)
        except Exception as exc:
            logger.debug("L2盘口指标获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or not isinstance(data, dict):
            return None
        metrics = compute_order_book_metrics(
            data,
            slippage_qty=self._cfg.order_book_slippage_qty,
        )
        scores: dict[str, float] = {}
        spread_bps = _safe_float_or_none(metrics.get("spread_bps"))
        if spread_bps is not None:
            scores["book_spread"] = features.order_book_spread_score(
                spread_bps,
                self._cfg.order_book_spread_warning_bps,
                self._cfg.order_book_spread_danger_bps,
            )
        buy_slippage = _safe_float_or_none(metrics.get("estimated_buy_slippage_bps"))
        sell_slippage = _safe_float_or_none(metrics.get("estimated_sell_slippage_bps"))
        slippages = [
            value for value in (buy_slippage, sell_slippage) if value is not None
        ]
        if slippages:
            scores["book_slippage"] = features.order_book_slippage_score(
                max(slippages),
                self._cfg.order_book_slippage_warning_bps,
                self._cfg.order_book_slippage_danger_bps,
            )
        if not scores:
            return None
        return {"scores": scores, "raw": metrics}

    def _l2_imbalance_score(self, code: str) -> tuple[float, dict] | None:
        """Score visible L2 book imbalance without using future snapshots."""

        level = max(self._cfg.order_book_levels, self._cfg.l2_imbalance_level)
        try:
            ret, data = self._data.get_order_book(code, level)
        except Exception as exc:
            logger.debug("L2 imbalance fetch failed %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or not isinstance(data, dict):
            return None
        metrics = compute_order_book_metrics(
            data,
            levels=metric_levels_for(self._cfg.l2_imbalance_level),
            slippage_qty=self._cfg.order_book_slippage_qty,
        )
        signal = evaluate_l2_imbalance(
            metrics,
            config=L2ImbalanceConfig(
                level=self._cfg.l2_imbalance_level,
                warn=self._cfg.l2_imbalance_warn,
                danger=self._cfg.l2_imbalance_danger,
                spread_warning_bps=self._cfg.order_book_spread_warning_bps,
                spread_danger_bps=self._cfg.order_book_spread_danger_bps,
                slippage_warning_bps=self._cfg.order_book_slippage_warning_bps,
                slippage_danger_bps=self._cfg.order_book_slippage_danger_bps,
            ),
            code=code,
        )
        return signal.score, {
            "imbalance": signal.imbalance,
            "direction": signal.direction,
            "risk_level": signal.risk_level,
            "reasons": list(signal.reasons),
            "metrics": metrics,
        }

    def _append_microstructure_blocks(
        self, scores: dict[str, float], reasons: list[str]
    ) -> None:
        """按已计算的实时微观结构分数追加买入门禁原因。"""
        threshold = self._cfg.microstructure_block_score
        for key in (
            "order_flow",
            "obi",
            "book_pressure",
            "book_spread",
            "book_slippage",
            "l2_imbalance",
            "lunch_continuation",
        ):
            score = scores.get(key)
            if score is not None and score >= threshold:
                reasons.append(f"{key} 实时风险偏高: score={score:.1f}")

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

    def _lunch_continuation_score(self, code: str) -> tuple[float, dict] | None:
        """计算港股午休前后短窗收益延续性。"""
        minute = self._intraday_minute(code)
        if minute is None or minute.empty or "time_key" not in minute.columns:
            return None
        try:
            rows = minute[["time_key", "close"]].copy()
            time_values = rows["time_key"].astype(str).str[11:16]
            rows = rows.assign(
                _time=time_values,
                _close=rows["close"].astype(float),
                _minute=time_values.apply(_minute_of_day),
            )
        except (KeyError, TypeError, ValueError):
            return None
        window = max(1, self._cfg.lunch_window_minutes)
        pre_start_min = 12 * 60 - window
        post_end_min = 13 * 60 + window
        pre = rows[
            rows["_minute"].notna()
            & (rows["_minute"] >= pre_start_min)
            & (rows["_minute"] < 12 * 60)
        ]
        post = rows[
            rows["_minute"].notna()
            & (rows["_minute"] >= 13 * 60)
            & (rows["_minute"] <= post_end_min)
        ]
        if len(pre) < 2 or len(post) < 2:
            return None
        pre_first = float(pre["_close"].iloc[0])
        pre_last = float(pre["_close"].iloc[-1])
        post_first = float(post["_close"].iloc[0])
        post_last = float(post["_close"].iloc[-1])
        if pre_first <= 0 or post_first <= 0:
            return None
        pre_ret = (pre_last - pre_first) / pre_first
        post_ret = (post_last - post_first) / post_first
        score = features.lunch_continuation_score(pre_ret, post_ret)
        return score, {"pre_return": pre_ret, "post_return": post_ret}

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

    def _hk_futures_filter_score(self) -> tuple[float, dict] | None:
        """恒指/国指期货过滤分；期货不可用时回退指数/ETF 代理。"""
        primary = self._trend_scores_for_symbols(
            self._cfg.hk_futures_symbols,
            self._cfg.hk_futures_filter_lookback_days,
        )
        detail: dict[str, list[dict] | str] = {"source": "futures", "items": primary}
        items = primary
        if not items:
            fallback = self._trend_scores_for_symbols(
                self._cfg.hk_futures_proxy_symbols,
                self._cfg.hk_futures_filter_lookback_days,
            )
            detail = {"source": "proxy", "items": fallback}
            items = fallback
        if not items:
            return None
        score = sum(float(item["score"]) for item in items) / len(items)
        return score, detail

    def _trend_scores_for_symbols(
        self, symbols: tuple[str, ...], lookback_days: int
    ) -> list[dict]:
        """为一组风险资产代理计算趋势过滤分。"""
        out: list[dict] = []
        for symbol in symbols:
            daily = self._daily_kline_for(symbol, lookback_days + 2)
            change = _change_from_daily(daily, lookback_days)
            if change is None:
                continue
            score = features.asset_trend_score(change, risk_on=True)
            out.append({"symbol": symbol, "change_pct": change, "score": score})
        return out

    def _daily_kline_for(self, code: str, bars: int) -> object | None:
        """拉取指定标的日线，供恒指/国指过滤使用。"""
        today = market_date(self._cfg.market_timezone)
        end = today.isoformat()
        start = (today - timedelta(days=bars * 3 + 10)).isoformat()
        try:
            ret, df, _ = self._data.request_history_kline(
                code, start=start, end=end, ktype=ft.KLType.K_DAY, max_count=bars
            )
        except Exception as exc:
            logger.debug("过滤器日线获取异常 %s: %s", code, exc)
            return None
        if ret != ft.RET_OK or df.empty:
            return None
        return df

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
            if "order_volume" in bid_df.columns and "order_volume" in ask_df.columns:
                bid_vol = (
                    float(bid_df["order_volume"].sum()) if not bid_df.empty else 0.0
                )
                ask_vol = (
                    float(ask_df["order_volume"].sum()) if not ask_df.empty else 0.0
                )
            else:
                bid_vol = float(len(bid_df))
                ask_vol = float(len(ask_df))
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
        days_to_expiry = (lockup_expiry - market_date(self._cfg.market_timezone)).days
        return 0 <= days_to_expiry <= cfg.lockup_warning_days


def _safe_float(value) -> float:
    try:
        result = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _safe_float_or_none(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


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


def _level_buckets(raw_levels: tuple[str, ...], default_level: int) -> list[int]:
    """解析多档盘口层级，保证至少包含 default_level。"""
    levels = {max(1, int(default_level))}
    for raw in raw_levels:
        try:
            level = int(raw)
        except (TypeError, ValueError):
            continue
        if level > 0:
            levels.add(level)
    return sorted(levels)


def _change_from_daily(daily, lookback_days: int) -> float | None:
    """从日线计算 lookback 收益率；数据不足或字段异常返回 None。"""
    if daily is None:
        return None
    try:
        closes = [float(x) for x in daily["close"]]
    except (KeyError, TypeError, ValueError):
        return None
    if len(closes) <= lookback_days:
        return None
    first = closes[-1 - lookback_days]
    last = closes[-1]
    if first <= 0:
        return None
    change = (last - first) / first
    return change if math.isfinite(change) else None


def _minute_of_day(value: str) -> int | None:
    """把 HH:MM 字符串转成日内分钟数。"""
    try:
        hour, minute = value.split(":", 1)
        return int(hour) * 60 + int(minute)
    except (AttributeError, ValueError):
        return None
