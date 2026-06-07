# -*- coding: utf-8 -*-
import os
from dataclasses import dataclass
from enum import Enum

from moomoo_rate_limits import DEFAULT_DATA_ACCESS_RATE_LIMIT, DEFAULT_RATE_WINDOW_S

_DEFAULT_WATCHLIST_FILE = os.path.join(os.path.dirname(__file__), "watchlist.txt")


def _csv_tuple(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    """从环境变量读取逗号分隔配置，空值回退默认 tuple。"""
    raw = os.environ.get(name, "")
    if not raw.strip():
        return default
    return tuple(x.strip() for x in raw.split(",") if x.strip())


def _trade_env_from_env() -> str:
    """读取交易环境并规范成大写；空值回退模拟盘。"""
    return (os.environ.get("TRADE_ENV", "SIMULATE").strip() or "SIMULATE").upper()


def _load_watchlist() -> tuple:
    """加载观察列表：WATCHLIST 环境变量优先，否则回退到 watchlist.txt。

    文件格式：每行一个代码，支持逗号分隔；'#' 起为注释，空行忽略。
    返回去重保序的代码元组；环境变量与文件均空时返回 ()（=仅 IPO 扫描）。
    """
    env = os.environ.get("WATCHLIST", "")
    if env.strip():
        raw = [c.strip() for c in env.split(",")]
    else:
        path = os.environ.get("WATCHLIST_FILE", _DEFAULT_WATCHLIST_FILE)
        if not os.path.exists(path):
            return ()
        raw = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    raw.extend(c.strip() for c in line.split(","))
    seen: set[str] = set()
    out: list[str] = []
    for code in raw:
        if code and code not in seen:
            seen.add(code)
            out.append(code)
    return tuple(out)


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
    allow_real_trading: bool = False  # REAL 模式额外防护开关，须 ALLOW_REAL_TRADING=yes

    # ── IPO 扫描 ────────────────────────────────────────────────────────
    ipo_days_window: int = 10  # 只关注上市后 N 天内的新股
    markets: tuple = ("US",)  # 本仓库专注美股

    # ── 通用 universe（除 IPO 扫描外，额外纳入的自选美股）──────────────
    # 空=仅 IPO 扫描（与历史行为一致）；填入即对任意美股做同一套因子分析。
    # from_env 经 _load_watchlist 填充：WATCHLIST 环境变量优先，否则读 watchlist.txt。
    # 自选标的无上市日 → 锁定期因子自动 no-op、换手率走"成熟股"阈值 profile。
    watchlist: tuple = ()  # 如 ("US.AAPL", "US.TSLA", "US.NVDA")

    # ── 交易时段（America/New_York，自动处理 EDT/EST）──────────────────
    market_timezone: str = "America/New_York"
    market_open: str = "09:30"
    market_close: str = "16:00"
    open_cooldown_minutes: int = 15  # 开盘后 N 分钟内不下买单

    # ── 仓位管理 ────────────────────────────────────────────────────────
    position_ratio: float = 0.2  # 满仓时每只股占购买力比例
    max_positions: int = 3  # 最多同时持仓股票数；<=0 表示不限制
    entry_tranches: int = 2  # 分批买入笔数（1=一次性全仓）
    exit_tranches: int = 1  # 分批卖出笔数（1=一次性清仓）

    # ── 波动率/ATR 仓位（替代固定 position_ratio）──────────────────────
    use_atr_sizing: bool = False  # True 时按 ATR 风险预算定仓位
    atr_period: int = 14  # ATR 计算周期（日线）
    atr_stop_multiple: float = 2.0  # 止损距离 = ATR × 该倍数
    atr_risk_per_trade_pct: float = 0.01  # 单笔最大风险占净值比例（1%）

    # ── 换手率阈值（按标的自动分 profile，见 signals._turnover_thresholds）─
    # IPO profile：新股首日换手率常 50%–300%
    turnover_warning: float = 80.0
    turnover_danger: float = 150.0
    # 成熟股 profile：日换手率通常 0.5%–10%，故阈值远低于 IPO
    general_turnover_warning: float = 5.0
    general_turnover_danger: float = 15.0

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

    # ── 盘中微观结构（美股 order-flow，替代港股 broker_queue）────────────
    # 三者均依赖实时订阅（TICKER/ORDER_BOOK）或 INTRADAY 资金流；无历史回放，
    # 须靠 forward-logging 前向校准后再赋权启用。默认全关、权重 0。
    use_order_flow: bool = False  # CVD 主动买卖盘失衡（get_rt_ticker）
    rt_ticker_num: int = 500  # 逐笔回看根数
    use_dark_pool_proxy: bool = False
    dark_pool_rt_ticker_num: int = 500
    dark_pool_us_min_notional: float = 100_000.0
    dark_pool_hk_min_notional: float = 800_000.0
    dark_pool_alert_cooldown_s: float = 300.0
    use_order_book_imbalance: bool = False  # 盘口失衡 OBI（get_order_book）
    obi_levels: int = 5  # 盘口累计档位数
    obi_level_buckets: tuple[str, ...] = ("1", "3", "5", "10")  # 多档 OBI 记录档位
    use_order_book_pressure: bool = False
    use_order_book_metrics: bool = False
    order_book_levels: int = 50
    order_book_slippage_qty: float = 1000.0
    order_book_cache_max_age_s: float = 3.0
    order_book_spread_warning_bps: float = 5.0
    order_book_spread_danger_bps: float = 30.0
    order_book_slippage_warning_bps: float = 10.0
    order_book_slippage_danger_bps: float = 50.0
    use_l2_imbalance_tracker: bool = False
    l2_imbalance_level: int = 10
    l2_imbalance_warn: float = 0.35
    l2_imbalance_danger: float = 0.60
    l2_imbalance_persist_snapshots: int = 3
    l2_imbalance_alert_cooldown_s: float = 300.0
    use_microstructure_gate: bool = False  # CVD/OBI 实时门禁，前向 IC 达标后再开
    microstructure_block_score: float = 70.0
    use_intraday_flow: bool = False  # 日内机构资金流斜率（get_capital_flow INTRADAY）
    flow_slope_window: int = 30  # 斜率回看的分钟根数

    # ── 做空面 ──────────────────────────────────────────────────────────
    use_short_metrics: bool = False  # 空头拥挤度 / 每日卖空比例
    short_squeeze_reverse: bool = False  # True=拥挤视为偏多（IC 校准为负时启用）

    # ── 期权隐含信息（IV skew / Put-Call Ratio；仅有期权的 IPO 可用）────
    use_option_iv: bool = False
    option_iv_max_expiry_days: int = 45  # 只取最近 N 天内到期的合约算 IV
    option_warning_score: float = 70.0  # 只产生风险提示，不进入买卖条件

    # ── 可回测买入门禁（默认关闭，Sharpe 验证后再打开）──────────────
    use_crypto_filter: bool = False
    crypto_filter_codes: tuple[str, ...] = ("US.COIN", "US.IREN")
    crypto_filter_symbols: tuple[str, ...] = ("CC.BTC", "CC.ETH", "US.IBIT")
    crypto_filter_lookback_days: int = 5
    crypto_filter_block_score: float = 70.0

    use_macro_filter: bool = False
    macro_risk_on_symbols: tuple[str, ...] = ("US.QQQ",)
    macro_risk_off_symbols: tuple[str, ...] = ("US.VIXY", "US.UVXY")
    macro_filter_lookback_days: int = 5
    macro_filter_block_score: float = 70.0

    # ── 因子权重（启用且数据可用的因子在评分时自动归一化）──────────────
    # 新因子默认权重 0：建议先用 analysis.py 做 IC 校准再启用。
    w_turnover: float = 0.25
    w_capital: float = 0.55
    w_momentum: float = 0.20
    w_broker: float = 0.15  # 仅 use_broker_signal=True 时参与
    w_orb: float = 0.20
    w_rs: float = 0.15
    w_vwap: float = 0.10
    w_order_flow: float = 0.0  # 微观结构：须前向校准后再赋权
    w_dark_pool_proxy: float = 0.0
    w_obi: float = 0.0
    w_book_pressure: float = 0.0
    w_book_spread: float = 0.0
    w_book_slippage: float = 0.0
    w_l2_imbalance: float = 0.0
    w_intraday_flow: float = 0.0
    w_short: float = 0.0  # 做空面
    w_option_iv: float = 0.0  # 期权隐含

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
    short_cache_ttl_s: float = 3600.0  # 做空数据低频（日/双月），长缓存
    option_cache_ttl_s: float = 300.0  # 期权链/到期日变动慢

    # ── API 限流（moomoo 约 30 次/30 秒，留余量）───────────────────────
    api_rate_limit: int = DEFAULT_DATA_ACCESS_RATE_LIMIT
    api_rate_window_s: float = DEFAULT_RATE_WINDOW_S

    # ── 回测成本模型 ────────────────────────────────────────────────────
    commission_per_share: float = 0.005  # 每股佣金（美股常见）
    commission_min: float = 1.0  # 单笔最低佣金
    slippage_bps: float = 5.0  # 滑点（基点，5bps=0.05%）
    backtest_benchmark: str = "US.SPY"  # 回测基准

    # ── 持久化 ──────────────────────────────────────────────────────────
    db_path: str = "us_strategy/positions.db"

    # ── 告警通知 ────────────────────────────────────────────────────────
    alert_email: str = ""  # 收件人，空字符串表示不发邮件
    alert_smtp_host: str = "smtp.gmail.com"
    alert_smtp_port: int = 587
    alert_smtp_user: str = ""
    alert_smtp_password: str = ""
    telegram_token: str = ""  # Telegram Bot Token，空表示不发送
    telegram_chat_id: str = ""
    feishu_chat_id: str = ""  # 飞书群 chat_id，空表示不发送
    lark_cli: str = "lark-cli"  # lark-cli 命令或绝对路径

    @classmethod
    def from_env(cls) -> "StrategyConfig":
        def _bool(name: str, default: bool) -> bool:
            return os.environ.get(name, str(default)).lower() == "true"

        return cls(
            host=os.environ.get("OPEND_HOST", "127.0.0.1"),
            port=int(os.environ.get("OPEND_PORT", "11111")),
            trade_password=os.environ.get("TRADE_PASSWORD", ""),
            trd_env=_trade_env_from_env(),
            allow_real_trading=os.environ.get("ALLOW_REAL_TRADING", "").lower()
            in ("yes", "true", "1"),
            ipo_days_window=int(os.environ.get("IPO_DAYS_WINDOW", "10")),
            watchlist=_load_watchlist(),
            general_turnover_warning=float(
                os.environ.get("GENERAL_TURNOVER_WARNING", "5.0")
            ),
            general_turnover_danger=float(
                os.environ.get("GENERAL_TURNOVER_DANGER", "15.0")
            ),
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
            api_rate_limit=int(
                os.environ.get("API_RATE_LIMIT", str(DEFAULT_DATA_ACCESS_RATE_LIMIT))
            ),
            api_rate_window_s=float(
                os.environ.get("API_RATE_WINDOW_S", str(DEFAULT_RATE_WINDOW_S))
            ),
            use_broker_signal=_bool("USE_BROKER_SIGNAL", False),
            use_orb=_bool("USE_ORB", False),
            use_rs=_bool("USE_RS", False),
            use_vwap_signal=_bool("USE_VWAP_SIGNAL", False),
            use_order_flow=_bool("USE_ORDER_FLOW", False),
            use_dark_pool_proxy=_bool("USE_DARK_POOL_PROXY", False),
            dark_pool_rt_ticker_num=int(
                os.environ.get("DARK_POOL_RT_TICKER_NUM", "500")
            ),
            dark_pool_us_min_notional=float(
                os.environ.get("DARK_POOL_US_MIN_NOTIONAL", "100000.0")
            ),
            dark_pool_hk_min_notional=float(
                os.environ.get("DARK_POOL_HK_MIN_NOTIONAL", "800000.0")
            ),
            dark_pool_alert_cooldown_s=float(
                os.environ.get("DARK_POOL_ALERT_COOLDOWN_S", "300.0")
            ),
            use_order_book_imbalance=_bool("USE_ORDER_BOOK_IMBALANCE", False),
            use_order_book_pressure=_bool("USE_ORDER_BOOK_PRESSURE", False),
            use_order_book_metrics=_bool("USE_ORDER_BOOK_METRICS", False),
            order_book_levels=int(os.environ.get("ORDER_BOOK_LEVELS", "50")),
            order_book_slippage_qty=float(
                os.environ.get("ORDER_BOOK_SLIPPAGE_QTY", "1000.0")
            ),
            order_book_cache_max_age_s=float(
                os.environ.get("ORDER_BOOK_CACHE_MAX_AGE_S", "3.0")
            ),
            order_book_spread_warning_bps=float(
                os.environ.get("ORDER_BOOK_SPREAD_WARNING_BPS", "5.0")
            ),
            order_book_spread_danger_bps=float(
                os.environ.get("ORDER_BOOK_SPREAD_DANGER_BPS", "30.0")
            ),
            order_book_slippage_warning_bps=float(
                os.environ.get("ORDER_BOOK_SLIPPAGE_WARNING_BPS", "10.0")
            ),
            order_book_slippage_danger_bps=float(
                os.environ.get("ORDER_BOOK_SLIPPAGE_DANGER_BPS", "50.0")
            ),
            use_l2_imbalance_tracker=_bool("USE_L2_IMBALANCE_TRACKER", False),
            l2_imbalance_level=int(os.environ.get("L2_IMBALANCE_LEVEL", "10")),
            l2_imbalance_warn=float(os.environ.get("L2_IMBALANCE_WARN", "0.35")),
            l2_imbalance_danger=float(os.environ.get("L2_IMBALANCE_DANGER", "0.60")),
            l2_imbalance_persist_snapshots=int(
                os.environ.get("L2_IMBALANCE_PERSIST_SNAPSHOTS", "3")
            ),
            l2_imbalance_alert_cooldown_s=float(
                os.environ.get("L2_IMBALANCE_ALERT_COOLDOWN_S", "300.0")
            ),
            w_book_pressure=float(os.environ.get("W_BOOK_PRESSURE", "0.0")),
            w_dark_pool_proxy=float(os.environ.get("W_DARK_POOL_PROXY", "0.0")),
            w_book_spread=float(os.environ.get("W_BOOK_SPREAD", "0.0")),
            w_book_slippage=float(os.environ.get("W_BOOK_SLIPPAGE", "0.0")),
            w_l2_imbalance=float(os.environ.get("W_L2_IMBALANCE", "0.0")),
            use_microstructure_gate=_bool("USE_MICROSTRUCTURE_GATE", False),
            microstructure_block_score=float(
                os.environ.get("MICROSTRUCTURE_BLOCK_SCORE", "70.0")
            ),
            use_intraday_flow=_bool("USE_INTRADAY_FLOW", False),
            use_short_metrics=_bool("USE_SHORT_METRICS", False),
            short_squeeze_reverse=_bool("SHORT_SQUEEZE_REVERSE", False),
            use_option_iv=_bool("USE_OPTION_IV", False),
            option_warning_score=float(os.environ.get("OPTION_WARNING_SCORE", "70.0")),
            use_crypto_filter=_bool("USE_CRYPTO_FILTER", False),
            crypto_filter_codes=_csv_tuple(
                "CRYPTO_FILTER_CODES", ("US.COIN", "US.IREN")
            ),
            crypto_filter_symbols=_csv_tuple(
                "CRYPTO_FILTER_SYMBOLS", ("CC.BTC", "CC.ETH", "US.IBIT")
            ),
            crypto_filter_lookback_days=int(
                os.environ.get("CRYPTO_FILTER_LOOKBACK_DAYS", "5")
            ),
            crypto_filter_block_score=float(
                os.environ.get("CRYPTO_FILTER_BLOCK_SCORE", "70.0")
            ),
            use_macro_filter=_bool("USE_MACRO_FILTER", False),
            macro_risk_on_symbols=_csv_tuple("MACRO_RISK_ON_SYMBOLS", ("US.QQQ",)),
            macro_risk_off_symbols=_csv_tuple(
                "MACRO_RISK_OFF_SYMBOLS", ("US.VIXY", "US.UVXY")
            ),
            macro_filter_lookback_days=int(
                os.environ.get("MACRO_FILTER_LOOKBACK_DAYS", "5")
            ),
            macro_filter_block_score=float(
                os.environ.get("MACRO_FILTER_BLOCK_SCORE", "70.0")
            ),
            db_path=os.environ.get("DB_PATH", "us_strategy/positions.db"),
            alert_email=os.environ.get("ALERT_EMAIL", ""),
            alert_smtp_host=os.environ.get("SMTP_HOST", "smtp.gmail.com"),
            alert_smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            alert_smtp_user=os.environ.get("SMTP_USER", ""),
            alert_smtp_password=os.environ.get("SMTP_PASSWORD", ""),
            telegram_token=os.environ.get("TELEGRAM_TOKEN", ""),
            telegram_chat_id=os.environ.get("TELEGRAM_CHAT_ID", ""),
            feishu_chat_id=os.environ.get("FEISHU_CHAT_ID")
            or os.environ.get("LARK_CHAT_ID", ""),
            lark_cli=os.environ.get("LARK_CLI", "lark-cli"),
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
        if self.use_order_flow:
            weights["order_flow"] = self.w_order_flow
        if self.use_dark_pool_proxy:
            weights["dark_pool_proxy"] = self.w_dark_pool_proxy
        if self.use_order_book_imbalance:
            weights["obi"] = self.w_obi
        if self.use_order_book_pressure:
            weights["book_pressure"] = self.w_book_pressure
        if self.use_order_book_metrics:
            weights["book_spread"] = self.w_book_spread
            weights["book_slippage"] = self.w_book_slippage
        if self.use_l2_imbalance_tracker:
            weights["l2_imbalance"] = self.w_l2_imbalance
        if self.use_intraday_flow:
            weights["intraday_flow"] = self.w_intraday_flow
        if self.use_short_metrics:
            weights["short"] = self.w_short
        return weights
