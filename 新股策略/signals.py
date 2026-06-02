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

    def __init__(self, data: DataAccess, config: StrategyConfig):
        self._data = data
        self._cfg = config
        self._listing_dates: dict[str, date] = {}

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

        # ── 换手率（核心）────────────────────────────────────────────
        scores["turnover"] = features.turnover_score(
            rate, cfg.turnover_warning, cfg.turnover_danger
        )

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

        # 至少要有换手率或资金分布之一，否则信号不可信
        if "turnover" not in scores and "capital" not in scores:
            logger.warning("核心因子均缺失，跳过 %s", code)
            return None

        composite = features.score_from_features(scores, cfg.active_weights())
        lockup_warning = self._check_lockup_warning(code)

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
