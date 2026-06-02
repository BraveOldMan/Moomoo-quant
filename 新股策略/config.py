# -*- coding: utf-8 -*-
import os
from dataclasses import dataclass
from enum import Enum


class Signal(Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"


@dataclass(frozen=True)
class StrategyConfig:
    # ── OpenD 连接 ──────────────────────────────────────────────────────
    host: str = "127.0.0.1"
    port: int = 11111
    trade_password: str = ""
    trd_env: str = "SIMULATE"  # "SIMULATE" 或 "REAL"

    # ── IPO 扫描 ────────────────────────────────────────────────────────
    ipo_days_window: int = 10  # 只关注上市后 N 天内的新股
    markets: tuple = ("US",)  # 本仓库专注美股

    # ── 交易时段（America/New_York，自动处理 EDT/EST）──────────────────
    market_timezone: str = "America/New_York"
    market_open: str = "09:30"
    market_close: str = "16:00"
    open_cooldown_minutes: int = 15  # 开盘后 N 分钟内不下买单

    # ── 仓位管理 ────────────────────────────────────────────────────────
    position_ratio: float = 0.2  # 满仓时每只股占购买力比例
    max_positions: int = 3  # 最多同时持仓股票数
    entry_tranches: int = 2  # 分批买入笔数（1=一次性全仓）
    exit_tranches: int = 1  # 分批卖出笔数（1=一次性清仓）

    # ── 波动率/ATR 仓位（替代固定 position_ratio）──────────────────────
    use_atr_sizing: bool = False  # True 时按 ATR 风险预算定仓位
    atr_period: int = 14  # ATR 计算周期（日线）
    atr_stop_multiple: float = 2.0  # 止损距离 = ATR × 该倍数
    atr_risk_per_trade_pct: float = 0.01  # 单笔最大风险占净值比例（1%）

    # ── 换手率阈值（美股 IPO 首日 50%–300%）──────────────────────────
    turnover_warning: float = 80.0
    turnover_danger: float = 150.0

    # ── 机构资金分布阈值 ────────────────────────────────────────────────
    inst_outflow_warning: float = 0.55
    inst_outflow_danger: float = 0.70

    # ── 价格动量信号 ────────────────────────────────────────────────────
    momentum_bars: int = 5  # 计算动量用的 K 线根数（日线）

    # ── 开盘区间突破 ORB（首日无均线可用，ORB 更适用新股）──────────────
    use_orb: bool = False
    orb_minutes: int = 30  # 开盘区间长度（分钟）

    # ── 相对强弱 RS（vs 基准 ETF）──────────────────────────────────────
    use_rs: bool = False
    rs_benchmark: str = "US.SPY"
    rs_lookback_days: int = 5

    # ── VWAP 偏离 ───────────────────────────────────────────────────────
    use_vwap_signal: bool = False

    # ── 因子权重（启用且数据可用的因子在评分时自动归一化）──────────────
    # 新因子默认权重 0：建议先用 analysis.py 做 IC 校准再启用。
    w_turnover: float = 0.25
    w_capital: float = 0.55
    w_momentum: float = 0.20
    w_broker: float = 0.15  # 仅 use_broker_signal=True 时参与
    w_orb: float = 0.20
    w_rs: float = 0.15
    w_vwap: float = 0.10

    # ── 锁定期预警 ──────────────────────────────────────────────────────
    lockup_days: int = 180  # 标准锁定期天数
    lockup_warning_days: int = 10  # 锁定期到期前 N 天开始预警

    # ── 流动性过滤 ──────────────────────────────────────────────────────
    min_daily_turnover_usd: float = 1_000_000  # 日成交额低于此值跳过

    # ── 综合风险分阈值（0-100）─────────────────────────────────────────
    buy_threshold: float = 35.0
    sell_threshold: float = 60.0

    # ── 止损 ────────────────────────────────────────────────────────────
    stop_loss_pct: float = 0.05  # 固定止损 5%
    use_trailing_stop: bool = True  # 启用浮动止损
    trailing_stop_pct: float = 0.08  # 从最高点回撤 8% 触发

    # ── PDT 保护（按交易日计算，非自然日）──────────────────────────────
    min_hold_days: int = 1  # 买入后至少持仓 N 个交易日（0=关闭）

    # ── 经纪队列信号（美股无经纪商身份，默认关闭）──────────────────────
    use_broker_signal: bool = False

    # ── 组合熔断 ────────────────────────────────────────────────────────
    daily_loss_limit_pct: float = 0.02  # 当日账户亏损超过 2% 暂停买入
    # 熔断基准：prev_close=前收净值 / day_open=当日开盘净值 / first_seen=进程当日首次观测
    circuit_breaker_baseline: str = "prev_close"

    # ── 执行（限价保护，替代裸市价单）──────────────────────────────────
    use_limit_orders: bool = True  # True=marketable-limit，False=市价单
    limit_price_tolerance_pct: float = 0.005  # 买高/卖低容忍带 ±0.5%
    order_fill_timeout_s: float = 10.0  # 等待成交的最长秒数
    order_poll_interval_s: float = 1.0  # 轮询订单状态间隔

    # ── 数据访问缓存 TTL（秒）─────────────────────────────────────────
    snapshot_cache_ttl_s: float = 2.0
    kline_cache_ttl_s: float = 60.0
    capital_cache_ttl_s: float = 30.0
    position_cache_ttl_s: float = 3.0

    # ── API 限流（moomoo 约 30 次/30 秒，留余量）───────────────────────
    api_rate_limit: int = 28
    api_rate_window_s: float = 30.0

    # ── 回测成本模型 ────────────────────────────────────────────────────
    commission_per_share: float = 0.005  # 每股佣金（美股常见）
    commission_min: float = 1.0  # 单笔最低佣金
    slippage_bps: float = 5.0  # 滑点（基点，5bps=0.05%）
    backtest_benchmark: str = "US.SPY"  # 回测基准

    # ── 持久化 ──────────────────────────────────────────────────────────
    db_path: str = "新股策略/positions.db"

    # ── 告警通知 ────────────────────────────────────────────────────────
    alert_email: str = ""  # 收件人，空字符串表示不发邮件
    alert_smtp_host: str = "smtp.gmail.com"
    alert_smtp_port: int = 587
    alert_smtp_user: str = ""
    alert_smtp_password: str = ""
    telegram_token: str = ""  # Telegram Bot Token，空表示不发送
    telegram_chat_id: str = ""

    @classmethod
    def from_env(cls) -> "StrategyConfig":
        def _bool(name: str, default: bool) -> bool:
            return os.environ.get(name, str(default)).lower() == "true"

        return cls(
            host=os.environ.get("OPEND_HOST", "127.0.0.1"),
            port=int(os.environ.get("OPEND_PORT", "11111")),
            trade_password=os.environ.get("TRADE_PASSWORD", ""),
            trd_env=os.environ.get("TRADE_ENV", "SIMULATE"),
            ipo_days_window=int(os.environ.get("IPO_DAYS_WINDOW", "10")),
            position_ratio=float(os.environ.get("POSITION_RATIO", "0.2")),
            max_positions=int(os.environ.get("MAX_POSITIONS", "3")),
            entry_tranches=int(os.environ.get("ENTRY_TRANCHES", "2")),
            use_atr_sizing=_bool("USE_ATR_SIZING", False),
            atr_risk_per_trade_pct=float(
                os.environ.get("ATR_RISK_PER_TRADE_PCT", "0.01")
            ),
            stop_loss_pct=float(os.environ.get("STOP_LOSS_PCT", "0.05")),
            trailing_stop_pct=float(os.environ.get("TRAILING_STOP_PCT", "0.08")),
            min_hold_days=int(os.environ.get("MIN_HOLD_DAYS", "1")),
            daily_loss_limit_pct=float(os.environ.get("DAILY_LOSS_LIMIT_PCT", "0.02")),
            circuit_breaker_baseline=os.environ.get(
                "CIRCUIT_BREAKER_BASELINE", "prev_close"
            ),
            use_limit_orders=_bool("USE_LIMIT_ORDERS", True),
            limit_price_tolerance_pct=float(
                os.environ.get("LIMIT_PRICE_TOLERANCE_PCT", "0.005")
            ),
            use_broker_signal=_bool("USE_BROKER_SIGNAL", False),
            use_orb=_bool("USE_ORB", False),
            use_rs=_bool("USE_RS", False),
            use_vwap_signal=_bool("USE_VWAP_SIGNAL", False),
            db_path=os.environ.get("DB_PATH", "新股策略/positions.db"),
            alert_email=os.environ.get("ALERT_EMAIL", ""),
            alert_smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            alert_smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            alert_smtp_user=os.environ.get("SMTP_USER", ""),
            alert_smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
        )

    def active_weights(self) -> dict[str, float]:
        """返回当前启用的因子权重（供 features 归一化使用）。"""
        weights = {
            "turnover": self.w_turnover,
            "capital": self.w_capital,
            "momentum": self.w_momentum,
        }
        if self.use_broker_signal:
            weights["broker"] = self.w_broker
        if self.use_orb:
            weights["orb"] = self.w_orb
        if self.use_rs:
            weights["rs"] = self.w_rs
        if self.use_vwap_signal:
            weights["vwap"] = self.w_vwap
        return weights
