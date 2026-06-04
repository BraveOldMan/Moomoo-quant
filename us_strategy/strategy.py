# -*- coding: utf-8 -*-
import logging
import threading
from dataclasses import dataclass
from datetime import date, timedelta

from .clock import market_date
from .config import Signal, StrategyConfig
from .market_calendar import is_trading_day
from .signals import SignalCalculator, SignalResult

logger = logging.getLogger(__name__)


@dataclass
class Decision:
    code: str
    signal: Signal
    score: float
    reason: str
    atr: float | None = None  # 供 trader 做 ATR 仓位

    def __str__(self) -> str:
        return (
            f"[{self.signal.value}] {self.code} (score={self.score:.1f}): {self.reason}"
        )


class IPOStrategy:
    """IPO 策略决策核心。所有持仓状态访问均加锁，可被推送/轮询多线程安全调用。"""

    def __init__(self, calculator: SignalCalculator, config: StrategyConfig):
        self._calc = calculator
        self._cfg = config
        self._lock = threading.RLock()
        # 加权成本：code -> [total_qty, total_cost]
        self._cost_basis: dict[str, list[float]] = {}
        self._buy_dates: dict[str, date] = {}
        self._peak_prices: dict[str, float] = {}
        self._tranches_bought: dict[str, int] = {}
        # 熔断状态
        self._circuit_breaker_active = False
        self._circuit_breaker_date: date | None = None
        self._daily_start_value = 0.0
        self._injected_baseline: float | None = None

    # ── 决策 ────────────────────────────────────────────────────────────
    def evaluate(self, code: str, current_price: float | None = None) -> Decision:
        result = self._calc.calculate(code, last_price=current_price)
        if result is None:
            return Decision(code, Signal.HOLD, 50.0, "数据不足，无法评估")

        logger.info("信号计算: %s", result)
        score = result.composite_score
        cfg = self._cfg

        with self._lock:
            avg_cost = self._avg_cost(code)
            has_position = avg_cost is not None

            # 更新浮动止损峰值
            if current_price is not None and has_position:
                peak = self._peak_prices.get(code, avg_cost)
                if current_price > peak:
                    self._peak_prices[code] = current_price

            # 固定止损（最高优先级）
            if current_price is not None and has_position and avg_cost > 0:
                if (avg_cost - current_price) / avg_cost >= cfg.stop_loss_pct:
                    loss_pct = (avg_cost - current_price) / avg_cost * 100
                    return self._sell_or_hold(
                        code,
                        score,
                        result.atr,
                        f"触发止损: 成本{avg_cost:.3f} 现价{current_price:.3f} 亏损{loss_pct:.1f}%",
                        f"止损触发但 PDT 保护生效，亏损 {loss_pct:.1f}%",
                    )

            # 浮动止损
            if current_price is not None and has_position and cfg.use_trailing_stop:
                peak = self._peak_prices.get(code, avg_cost)
                if peak > 0 and (peak - current_price) / peak >= cfg.trailing_stop_pct:
                    dd = (peak - current_price) / peak * 100
                    return self._sell_or_hold(
                        code,
                        score,
                        result.atr,
                        f"触发浮动止损: 最高{peak:.3f} 现价{current_price:.3f} 回撤{dd:.1f}%",
                        f"浮动止损触发但 PDT 保护生效，回撤 {dd:.1f}%",
                    )

            # 出货信号（综合分高或锁定期临近）
            if score >= cfg.sell_threshold or result.lockup_warning:
                return self._sell_or_hold(
                    code,
                    score,
                    result.atr,
                    self._sell_reason(result),
                    f"出货信号触发但 PDT 保护生效（需持仓至少 {cfg.min_hold_days} 交易日）",
                )

            # 买入信号
            tranches = self._tranches_bought.get(code, 0)
            if (
                score < cfg.buy_threshold
                and tranches < cfg.entry_tranches
                and result.liquidity_ok
                and not self._circuit_breaker_active
            ):
                return Decision(
                    code,
                    Signal.BUY,
                    score,
                    self._buy_reason(result, tranches),
                    result.atr,
                )

            # HOLD
            if not result.liquidity_ok:
                return Decision(
                    code,
                    Signal.HOLD,
                    score,
                    "流动性不足（日成交额低于阈值）",
                    result.atr,
                )
            if self._circuit_breaker_active:
                return Decision(
                    code, Signal.HOLD, score, "组合熔断激活，当日暂停买入", result.atr
                )
            return Decision(
                code,
                Signal.HOLD,
                score,
                f"综合风险适中 (score={score:.1f})，观望",
                result.atr,
            )

    def _sell_or_hold(
        self,
        code: str,
        score: float,
        atr: float | None,
        sell_reason: str,
        hold_reason: str,
    ) -> Decision:
        if not self._can_sell(code):
            return Decision(code, Signal.HOLD, score, hold_reason, atr)
        return Decision(code, Signal.SELL, score, sell_reason, atr)

    # ── 熔断 ────────────────────────────────────────────────────────────
    def set_daily_baseline(self, value: float) -> None:
        """注入当日基准净值（prev_close / day_open 模式由 main 在开盘时调用）。"""
        with self._lock:
            self._injected_baseline = value

    def check_and_update_circuit_breaker(
        self,
        current_portfolio_value: float,
        current_date: date | None = None,
    ) -> bool:
        """按市场交易日更新组合熔断状态。"""
        with self._lock:
            today = current_date or market_date(self._cfg.market_timezone)
            if self._circuit_breaker_date != today:
                self._circuit_breaker_active = False
                self._circuit_breaker_date = today
                mode = self._cfg.circuit_breaker_baseline
                if mode in ("prev_close", "day_open") and self._injected_baseline:
                    self._daily_start_value = self._injected_baseline
                else:
                    if mode != "first_seen":
                        logger.warning("熔断基准 %s 未注入，降级为 first_seen", mode)
                    self._daily_start_value = current_portfolio_value
                self._injected_baseline = None
                return False

            if self._daily_start_value > 0 and not self._circuit_breaker_active:
                loss = (
                    self._daily_start_value - current_portfolio_value
                ) / self._daily_start_value
                if loss >= self._cfg.daily_loss_limit_pct:
                    self._circuit_breaker_active = True
                    logger.warning(
                        "组合熔断激活: 当日亏损 %.2f%%（限制 %.2f%%）",
                        loss * 100,
                        self._cfg.daily_loss_limit_pct * 100,
                    )
                    return True
            return False

    # ── 持仓状态 ────────────────────────────────────────────────────────
    def record_buy(
        self,
        code: str,
        price: float,
        qty: float,
        buy_date: date | None = None,
    ) -> None:
        """记录一笔已确认成交的买入。"""
        with self._lock:
            tot_q, tot_c = self._cost_basis.get(code, [0.0, 0.0])
            self._cost_basis[code] = [tot_q + qty, tot_c + price * qty]
            self._buy_dates.setdefault(
                code, buy_date or market_date(self._cfg.market_timezone)
            )
            self._peak_prices[code] = max(price, self._peak_prices.get(code, price))
            self._tranches_bought[code] = self._tranches_bought.get(code, 0) + 1

    def clear_position(self, code: str) -> None:
        with self._lock:
            self._cost_basis.pop(code, None)
            self._buy_dates.pop(code, None)
            self._peak_prices.pop(code, None)
            self._tranches_bought.pop(code, None)

    def restore_position(
        self,
        code: str,
        avg_cost: float,
        qty: float,
        buy_date: date,
        tranches_bought: int,
        peak_price: float,
    ) -> None:
        with self._lock:
            self._cost_basis[code] = [qty, avg_cost * qty]
            self._buy_dates[code] = buy_date
            self._peak_prices[code] = peak_price
            self._tranches_bought[code] = tranches_bought

    def get_active_codes(self) -> set[str]:
        with self._lock:
            return set(self._cost_basis.keys())

    def get_tranches_bought(self, code: str) -> int:
        with self._lock:
            return self._tranches_bought.get(code, 0)

    def get_peak_price(self, code: str) -> float:
        with self._lock:
            avg = self._avg_cost(code) or 0.0
            return self._peak_prices.get(code, avg)

    def get_avg_cost(self, code: str) -> float:
        with self._lock:
            return self._avg_cost(code) or 0.0

    def get_qty(self, code: str) -> float:
        with self._lock:
            tot_q, _ = self._cost_basis.get(code, [0.0, 0.0])
            return tot_q

    def has_position(self, code: str) -> bool:
        with self._lock:
            return code in self._cost_basis

    # ── 内部 ────────────────────────────────────────────────────────────
    def _avg_cost(self, code: str) -> float | None:
        basis = self._cost_basis.get(code)
        if basis is None or basis[0] <= 0:
            return None
        return basis[1] / basis[0]

    def _can_sell(self, code: str, current_date: date | None = None) -> bool:
        if self._cfg.min_hold_days <= 0:
            return True
        buy_date = self._buy_dates.get(code)
        if buy_date is None:
            return True
        today = current_date or market_date(self._cfg.market_timezone)
        return _trading_days_between(buy_date, today) >= self._cfg.min_hold_days

    def _buy_reason(self, r: SignalResult, current_tranches: int) -> str:
        parts = [
            f"综合风险低 (score={r.composite_score:.1f})",
            f"第{current_tranches + 1}/{self._cfg.entry_tranches}批",
        ]
        if r.capital_score < 30:
            parts.append("机构净买入")
        if r.turnover_rate < self._cfg.turnover_warning:
            parts.append(f"换手率健康 ({r.turnover_rate:.1f}%)")
        if r.momentum_score < 40:
            parts.append("价格动量向上")
        if r.scores.get("orb", 50) < 40:
            parts.append("开盘区间上破")
        if r.scores.get("rs", 50) < 40:
            parts.append("跑赢基准")
        return "，".join(parts)

    def _sell_reason(self, r: SignalResult) -> str:
        parts = []
        if r.lockup_warning:
            parts.append("锁定期临近到期（机构减持风险）")
        if r.capital_score >= self._cfg.sell_threshold:
            parts.append("机构大量出货（超大/大单净卖出）")
        if r.turnover_rate >= self._cfg.turnover_danger:
            parts.append(f"换手率过高 ({r.turnover_rate:.1f}%)")
        if r.broker_score >= 65:
            parts.append("卖方经纪队列占优")
        if r.momentum_score >= 70:
            parts.append("价格动量下行")
        if r.scores.get("rs", 50) >= 70:
            parts.append("跑输基准")
        if not parts:
            parts.append(f"综合风险过高 (score={r.composite_score:.1f})")
        return "，".join(parts)


def _trading_days_between(start: date, end: date) -> int:
    """start 之后到 end（含）之间的 NYSE 交易日数。同日或未来返回 0。"""
    if end <= start:
        return 0
    days = 0
    d = start + timedelta(days=1)
    while d <= end:
        if is_trading_day(d):
            days += 1
        d += timedelta(days=1)
    return days
