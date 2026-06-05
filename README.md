# Moomoo-quant · 美股多因子量化交易系统

基于 **moomoo OpenD 网关**（原 Futu API）的美股（US）程序化交易系统：从行情采集、多因子评分、决策（PDT/熔断/加权成本）、限价执行、持仓恢复，到回测、因子 IC 校准、前向样本采集与每日自动体检，形成完整闭环：

```
采集 → 评分 → 决策 → 执行 → 持久化 → 校准 → 再优化
```

> **市场范围**：仅美股，股票代码统一 `US.` 前缀（如 `US.AAPL`、`US.TSLA`）。不涉及 A 股。

---

## 目录

- [前置条件与安装](#前置条件与安装)
- [快速开始](#快速开始)
- [系统架构](#系统架构)
- [模块清单](#模块清单)
- [因子体系](#因子体系)
- [因子校准闭环](#因子校准闭环)
- [配置（环境变量）](#配置环境变量)
- [数据持久化](#数据持久化)
- [测试](#测试)
- [关键约定](#关键约定)
- [自动化计划任务](#自动化计划任务)
- [风险提示](#风险提示)

---

## 前置条件与安装

1. **启动 moomoo OpenD 网关**（API 必须经其中转，不直连交易所）
   - 默认地址 `127.0.0.1:11111`
   - 下载：<https://openapi.moomoo.com/moomoo-api-doc/en/quick/opend-base.html>
2. **安装依赖**
   ```bash
   pip install moomoo-api          # 或 pip install -e MMAPI4Python_10.6.6608/
   ```
   - 核心：`pandas`、`simplejson`、`protobuf>=3.20.0`、`PyCryptodome`
   - 策略可选：`talib`（技术指标）

## 快速开始

```bash
# 数据可得性探针（上线前必跑，确认美股各接口字段可用）
python -m us_strategy.probe US.RDDT US.ARM

# 运行策略（模拟盘；自动加载 watchlist.txt + IPO 扫描）
python -m us_strategy.main

# 临时覆盖观察列表
WATCHLIST=US.AAPL,US.TSLA python -m us_strategy.main

# 回测
python -m us_strategy.backtest

# 前向日志采集（只评分写库、不下单，为因子校准攒样本）
python -m us_strategy.forward_monitor

# 前向 IC 累计体检
python -m us_strategy.ic_report

# 运行单测（无需 OpenD）
pytest us_strategy/tests/ -q
```

## 系统架构

### 通信架构

```
策略代码 → moomoo Python SDK → TCP(11111) → OpenD 网关 → 交易所/行情源
```

两大核心 Context（均继承 `OpenContextBase`，管理连接/心跳/重连）：

| 类 | 用途 |
|---|---|
| `OpenQuoteContext` | 行情订阅、历史 K 线、快照、筛股、微观结构 |
| `OpenSecTradeContext` | 下单、查仓位/账户/成交 |

### 数据流

- **同步查询**：阻塞返回 `(ret_code, DataFrame)`，须先检查 `ret_code == RET_OK`
- **异步推送**：订阅后回调，继承 `*HandlerBase` 重写 `on_recv_rsp()`

### 策略分层（`us_strategy/`）

```
main.py            单线程事件队列编排（推送+轮询统一投递，串行消费，无并发下单竞态）
                   universe = IPO 扫描 ∪ 自选清单(WATCHLIST) ∪ 现有持仓
  ├─ data_access.py    TTL 缓存 + 令牌桶限流的行情/交易数据门面（防撞频）
  ├─ signals.py        经 data_access 取数 → 调 features 评分；缺失因子自动降级
  │   └─ features.py   统一特征 + 纯函数评分（实盘/回测共用）
  ├─ strategy.py       决策核心：加权成本、PDT、熔断基准锚定、RLock 加锁
  ├─ trader.py         marketable-limit 限价执行 + 成交轮询 + 新开/加仓区分
  ├─ persistence.py    SQLite 持仓恢复 + SignalLogStore 前向信号日志
  ├─ market_calendar.py / clock.py   NYSE 假日表 + 纽约市场日工具
  └─ alerts.py / monitor.py          多渠道告警 / 实时行情订阅

backtest.py        同源回测 + 佣金/滑点 + SPY 基准/Alpha + Sharpe/Sortino/Calmar + walk-forward
analysis.py        因子 IC/IR、分层回测、锁定期事件研究(CAR) + forward_ic_from_log（微观因子前向校准）
probe.py           数据可得性探针
forward_monitor.py 前向日志采集（只写 signal_log，不下单）
ic_report.py       每日 IC 累计体检（signal_log → ic_history，自动判定因子是否够格赋权）
```

## 模块清单

| 模块 | 职责 |
|---|---|
| `main.py` | 入口；单线程事件循环编排 |
| `config.py` | `StrategyConfig`（环境变量加载、因子开关、`active_weights()`）|
| `data_access.py` | 数据门面：TTL 缓存 + 令牌桶限流，封装快照/K线/逐笔/盘口/资金流/做空/期权链 |
| `features.py` | 纯函数因子评分（0–100 风险分）|
| `signals.py` | 取数→评分→`SignalResult`；缺失因子降级；universe profile |
| `strategy.py` | 决策核心：加权成本、PDT、熔断、止损 |
| `trader.py` | 限价执行 + 成交轮询 |
| `persistence.py` | `PositionStore`（持仓）+ `SignalLogStore`（信号日志）|
| `monitor.py` | 实时行情订阅（QUOTE + 可选 TICKER/ORDER_BOOK）|
| `alerts.py` | 邮件 / Telegram 等多渠道告警 |
| `market_calendar.py` / `clock.py` | NYSE 假日 / 纽约时区工具 |
| `backtest.py` | 回测引擎（成本、基准、风险指标、walk-forward）|
| `analysis.py` | 因子有效性分析（IC/IR、分层、CAR、前向 IC）|
| `probe.py` | 数据可得性探针 |
| `forward_monitor.py` | 前向日志采集 |
| `ic_report.py` | 前向 IC 累计体检 |

## 因子体系

所有 `*_score` 返回 **0–100 风险分**（0 = 低风险/偏多，100 = 高风险/偏空）。有效因子的 IC 应**显著为负**。

| 组别 | 因子（`scores` 键）| 数据源 | 状态 |
|---|---|---|---|
| 核心 | turnover / capital / momentum | snapshot / capital_distribution | ✅ 已赋权 |
| 技术 | orb / rs / vwap / ATR 仓位 | history_kline | 默认关闭、权重 0 |
| 盘中微观结构 | order_flow(CVD) / obi / intraday_flow | rt_ticker / order_book / capital_flow | 默认关闭、权重 0 |
| 做空面 | short | short_interest / daily_short_volume | 默认关闭、权重 0 |
| 期权隐含 | option_iv（IV skew + PCR）| option_chain + 期权 snapshot | 默认关闭、权重 0 |

**生产决策权重**（`config.active_weights()`）：`capital 0.55 / turnover 0.25 / momentum 0.20`。
扩展因子**必须先经 IC 校准（显著为负）才赋非零权重**，否则一律权重 0、不入决策。

## 因子校准闭环

微观结构因子无历史回放，只能前向收集校准：

```
forward_monitor.py  每交易日盘中循环评分 → 写 signal_log (scores@T, price@T)
        │           (扩展因子 force-enable，仅记录，不下单)
        ▼
ic_report.py        每日收盘后：每个交易日算一个横截面前向 IC(15/30min)
        │           → 存 ic_history 表 → 跨日聚合 meanIC / IR(=均值/标准差)
        ▼
赋权闸门            ≥20 个交易日 且 |meanIC|>0.03 且 |IR|>0.5 且符号为负
                    → "✅ 达标(可赋权)"；否则 "积累中 / 未达标 / 符号反(考虑反向)"
```

## 配置（环境变量）

均可选、有默认值。完整列表见 `us_strategy/main.py` 顶部 docstring。

| 类别 | 变量 |
|---|---|
| 连接 | `OPEND_HOST` / `OPEND_PORT` |
| 交易环境 | `TRADE_ENV`(SIMULATE/REAL) / `ALLOW_REAL_TRADING` / `TRADE_PASSWORD` |
| universe | `WATCHLIST` / `WATCHLIST_FILE` / `IPO_DAYS_WINDOW` |
| 仓位 | `POSITION_RATIO` / `MAX_POSITIONS` / `ENTRY_TRANCHES` / `USE_ATR_SIZING` |
| 风控 | `STOP_LOSS_PCT` / `TRAILING_STOP_PCT` / `MIN_HOLD_DAYS`(PDT) / `DAILY_LOSS_LIMIT_PCT` / `CIRCUIT_BREAKER_BASELINE` |
| 执行 | `USE_LIMIT_ORDERS` / `LIMIT_PRICE_TOLERANCE_PCT` |
| 因子开关 | `USE_RS` / `USE_ORB` / `USE_VWAP_SIGNAL` / `USE_ORDER_FLOW` / `USE_ORDER_BOOK_IMBALANCE` / `USE_INTRADAY_FLOW` / `USE_SHORT_METRICS` / `USE_OPTION_IV` |
| 告警 | `ALERT_EMAIL` / `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` |
| 校准 | `MONITOR_INTERVAL_S` / `MONITOR_MAX_ROUNDS` / `IC_HORIZONS` / `IC_MIN_DAYS` / `DB_PATH` |

## 数据持久化

SQLite（默认 `us_strategy/positions.db`，已 gitignore）：

| 表 | 用途 |
|---|---|
| `positions` | 持仓恢复（含数量、加权成本）|
| `signal_log` | 前向信号日志：`ts / code / last_price / scores(JSON)` |
| `ic_history` | 每日因子 IC：`date / factor / horizon_min / ic / n` |

## 测试

```bash
pytest us_strategy/tests/ -q            # 103 项纯逻辑单测，无需 OpenD
pytest --cov=us_strategy --cov-report=term-missing
```

实盘/回测**同源因子引擎**，单测覆盖因子评分、决策逻辑、回测指标、日历/持仓、数据质量、IC 体检等。

## 关键约定

- **股票代码**：`MARKET.CODE`，统一 `US.` 前缀
- **返回值**：所有 API 返回 `(ret_code, data)`，须先判 `ret_code == RET_OK`
- **评分**：0–100 风险分，有效因子 IC 显著为负
- **稳健默认**：新因子默认关闭、权重 0，须 IC 校准后启用；限价执行默认开启
- **实盘解锁**：实盘交易前必须 `trade_ctx.unlock_trade(password)`，且 `ALLOW_REAL_TRADING=yes`
- **数据可得性**：`get_capital_distribution`、`get_broker_queue` 在美股可能不可用，上线前先跑 `probe`

## 自动化计划任务

Windows 任务计划程序（不在仓库，启动器在 `us_strategy/*.ps1`）：

| 任务 | 触发 | 作用 |
|---|---|---|
| `MoomooForwardCollect` | 每周一~五 21:00 北京（覆盖 EDT/EST RTH）| 盘中前向采集，写 `signal_log`（需 OpenD）|
| `MoomooICReport` | 每周二~六 06:30 北京（收盘后）| IC 累计体检，写 `ic_history`（只读，无需 OpenD）|
| `MoomooHKForwardCollect` | 每周一~五 09:15 北京（HKT，含午休跳过）| 港股盘中前向采集（需 OpenD）|
| `MoomooHKICReport` | 每周一~五 16:30 北京（HK 收盘后）| 港股 IC 累计体检（只读，无需 OpenD）|

## 港股版 `hk_strategy/`

`hk_strategy/` 是 `us_strategy/` 的港股（HKEX）平行实现，复用同一套因子/决策/执行/回测/校准引擎，仅做市场特化：

| 维度 | 美股 `us_strategy` | 港股 `hk_strategy` |
|---|---|---|
| 代码前缀 | `US.`（如 `US.AAPL`）| `HK.`（5 位补零，如 `HK.00700`）|
| 时区 | America/New_York（夏令时）| Asia/Hong_Kong（无夏令时）|
| 交易时段 | 09:30–16:00 连续 | 09:30–12:00 + 午休 + 13:00–16:00 |
| 交易日历 | NYSE（规则可算）| HKEX（`request_trading_days` API 优先 + 硬编码兜底）|
| PDT | `min_hold_days=1` | 无 PDT，`min_hold_days=0` |
| 成本模型 | 每股佣金 | 成交额% + 印花税 + 交易所费 |
| 基准 | `US.SPY` | `HK.800000`（恒指）|
| 板手 | 1 股 | 一手 N 股（自动取 `lot_size`，已支持）|
| 独立 DB | `us_strategy/positions.db` | `hk_strategy/positions.db` |

```bash
python -m hk_strategy.probe HK.00700 HK.09988   # 数据可得性探针（需 OpenD）
python -m hk_strategy.main                        # 港股策略（自动加载 watchlist.txt + IPO 扫描）
pytest hk_strategy/tests/ -q                       # 港股单测（无需 OpenD）
```

> ⚠️ HKEX 硬编码假日表（`market_calendar.py`，2025–2027）为人工录入、需逐年核对官方历；生产以 API 刷新为准。扩展因子须在港股样本上**重新** IC 校准，不沿用美股结论。

## 风险提示

- 本项目为量化策略研究/开发框架，**不构成投资建议**。
- 实盘交易有资金损失风险；上线前务必在模拟环境（`TRADE_ENV=SIMULATE`）充分验证。
- 扩展因子在 IC 校准达标前权重为 0，等权探索分仅供观察，**不可直接据以交易**。

---

> 更多设计细节见 [`CLAUDE.md`](CLAUDE.md)（开发指引）与 [`us_strategy/REVIEW.md`](us_strategy/REVIEW.md)（升级记录）。
