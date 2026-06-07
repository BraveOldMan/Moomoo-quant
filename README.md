# Moomoo-quant · 美股/港股多因子量化交易系统

> 当前版本：v1.4.0（2026-06-08）

基于 **moomoo OpenD 网关**（原 Futu API）的美股（US）与港股（HK）程序化交易系统：从行情采集、多因子评分、决策（PDT/港股午休/熔断/加权成本）、限价执行、持仓恢复，到回测、因子 IC 校准、前向样本采集与每日自动体检，形成完整闭环：

```
采集 → 评分 → 决策 → 执行 → 持久化 → 校准 → 再优化
```

> **市场范围**：`us_strategy/` 面向美股，股票代码统一 `US.` 前缀（如 `US.AAPL`、`US.TSLA`）；`hk_strategy/` 面向港股，股票代码统一 `HK.` 前缀且 5 位补零（如 `HK.00700`、`HK.09988`）。不涉及 A 股。

---

## 目录

- [前置条件与安装](#前置条件与安装)
- [快速开始](#快速开始)
- [系统架构](#系统架构)
- [模块清单](#模块清单)
- [因子体系](#因子体系)
- [因子校准闭环](#因子校准闭环)
- [配置（环境变量）](#配置环境变量)
- [moomoo API 限频](#moomoo-api-限频)
- [v1.4.0 变更摘要](#v140-变更摘要)
- [数据持久化](#数据持久化)
- [测试](#测试)
- [关键约定](#关键约定)
- [自动化计划任务](#自动化计划任务)
- [港股版 hk_strategy](#港股版-hk_strategy)
- [风险提示](#风险提示)

---

## 前置条件与安装

1. **启动 moomoo OpenD 网关**（API 必须经其中转，不直连交易所）
   - 默认地址 `127.0.0.1:11111`
   - 下载：<https://openapi.moomoo.com/moomoo-api-doc/en/quick/opend-base.html>
2. **安装依赖**
   ```bash
   pip install moomoo-api          # 或 pip install -e MMAPI4Python_10.7.6708/
   ```
   - 核心：`pandas`、`simplejson`、`protobuf>=3.20.0`、`PyCryptodome`
   - 策略可选：`talib`（技术指标）

## 快速开始

```bash
# 美股数据可得性探针（上线前必跑，确认各接口字段可用）
python -m us_strategy.probe US.RDDT US.ARM

# 港股数据可得性探针（上线前必跑，确认各接口字段可用）
python -m hk_strategy.probe HK.00700 HK.09988

# 运行美股策略（模拟盘；自动加载 watchlist.txt + IPO 扫描）
python -m us_strategy.main

# 运行港股策略（模拟盘；自动加载 watchlist.txt + IPO 扫描）
python -m hk_strategy.main

# 临时覆盖观察列表（按目标策略使用 US. 或 HK. 前缀）
WATCHLIST=US.AAPL,US.TSLA python -m us_strategy.main
WATCHLIST=HK.00700,HK.09988 python -m hk_strategy.main

# 回测
python -m us_strategy.backtest
python -m hk_strategy.backtest

# 前向日志采集（只评分写库、不下单，为因子校准攒样本）
python -m us_strategy.forward_monitor
python -m hk_strategy.forward_monitor

# 前向 IC 累计体检
python -m us_strategy.ic_report
python -m hk_strategy.ic_report

# 运行单测（无需 OpenD）
pytest us_strategy/tests/ -q
pytest hk_strategy/tests/ -q
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
| 盘中微观结构 | order_flow(CVD) / dark_pool_proxy / obi / l2_imbalance / intraday_flow | rt_ticker / order_book / capital_flow | 默认关闭、权重 0 |
| 港股微观状态 | broker / hk_status | broker_queue / snapshot dark_status / snapshot sec_status | 默认关闭、权重 0 |
| 做空面 | short | short_interest / daily_short_volume | 默认关闭、权重 0 |
| 期权隐含 | option_iv（IV skew + PCR）| option_chain + 期权 snapshot | 默认关闭、权重 0 |

**生产决策权重**（`config.active_weights()`）：`capital 0.55 / turnover 0.25 / momentum 0.20`。
扩展因子**必须先经 IC 校准（显著为负）才赋非零权重**，否则一律权重 0、不入决策。
`dark_pool_proxy` 只是 moomoo 可见逐笔的大额成交代理信号，不是 FINRA TRF 暗池认证数据。

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

均可选、有默认值。美股完整列表见 `us_strategy/main.py` 顶部 docstring；港股完整列表见 `hk_strategy/main.py` 顶部 docstring。

| 类别 | 变量 |
|---|---|
| 连接 | `OPEND_HOST` / `OPEND_PORT` |
| 交易环境 | `TRADE_ENV`(SIMULATE/REAL) / `ALLOW_REAL_TRADING` / `TRADE_PASSWORD` |
| universe | `WATCHLIST` / `WATCHLIST_FILE` / `IPO_DAYS_WINDOW` |
| 港股流动性 | `MIN_DAILY_TURNOVER` |
| 仓位 | `POSITION_RATIO` / `MAX_POSITIONS` / `ENTRY_TRANCHES` / `USE_ATR_SIZING` |
| 风控 | `STOP_LOSS_PCT` / `TRAILING_STOP_PCT` / `MIN_HOLD_DAYS`(PDT) / `DAILY_LOSS_LIMIT_PCT` / `CIRCUIT_BREAKER_BASELINE` |
| 执行 | `USE_LIMIT_ORDERS` / `LIMIT_PRICE_TOLERANCE_PCT` |
| 因子开关 | `USE_RS` / `USE_ORB` / `USE_VWAP_SIGNAL` / `USE_ORDER_FLOW` / `USE_DARK_POOL_PROXY` / `USE_ORDER_BOOK_IMBALANCE` / `USE_L2_IMBALANCE_TRACKER` / `USE_INTRADAY_FLOW` / `USE_SHORT_METRICS` / `USE_OPTION_IV` / `USE_BROKER_SIGNAL` / `USE_BROKER_GATE` / `USE_HK_STATUS_SIGNAL` |
| 告警 | `ALERT_EMAIL` / `TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` |
| 校准 | `MONITOR_INTERVAL_S` / `MONITOR_MAX_ROUNDS` / `IC_HORIZONS` / `IC_MIN_DAYS` / `DB_PATH` |

## moomoo API 限频

仓库级限频事实表在 `moomoo_rate_limits.py`，US/HK `StrategyConfig` 的默认 `api_rate_limit=28`、`api_rate_window_s=30` 从该表读取；这是给 `DataAccess` 的保守全局桶，覆盖资金流/资金分布等 30 次/30 秒接口并保留余量。可用 `API_RATE_LIMIT`、`API_RATE_WINDOW_S` 覆盖，但调高前必须按接口单独核对。

高频接口默认规则：

| 接口 | 官方/仓库规则 | 备注 |
|---|---:|---|
| `get_market_snapshot` | 60 次 / 30 秒；建议间隔 0.5 秒 | 单次最多 400 个标的；HK BMP 权限有更低单次数量上限 |
| `request_history_kline` | 60 次 / 30 秒；建议间隔 0.5 秒 | 分页时只限制每只股票首页，后续页不计入该限频 |
| `get_capital_flow` / `get_capital_distribution` | 30 次 / 30 秒；建议间隔 1 秒 | `DataAccess` 默认 28/30 秒覆盖这组接口 |
| `get_option_expiration_date` | 60 次 / 30 秒；建议间隔 0.5 秒 | 期权链前置查询 |
| `get_option_chain` | 10 次 / 30 秒；建议间隔 3 秒 | 日报和历史回填默认按 3 秒节流 |
| `position_list_query` / `accinfo_query` / `order_list_query` | 10 次 / 30 秒 / 账户 | 只有 `refresh_cache=True` 时触发限频；策略默认读 OpenD 缓存 |
| `place_order` | 15 次 / 30 秒 / 账户，连续请求间隔不低于 0.02 秒 | 实盘仍需人工确认，脚本不得自动启动实盘 |
| `get_stock_quote` / `get_order_book` / `get_rt_ticker` / `get_rt_data` / `get_cur_kline` / `get_broker_queue` | 读取订阅后的 OpenD 推送缓存 | 不按服务器请求限频计算，但受订阅额度和数据权限约束 |

没有在当前官方页面发布单接口频率的低频查询，统一在 `moomoo_rate_limits.py` 标记为 `official=False`，仓库默认按 30 次/30 秒保守处理，不允许把未知接口默认提到高频。

## v1.4.0 变更摘要

本版本聚焦 US/HK 观察列表数据落库、盘中微观结构采集和港股状态信号前向校准，不自动调整实盘权重，不改变实盘启动红线。

| 方向 | 变更 |
|---|---|
| 实时行情落库 | `tools.collect_moomoo_ticks` 新增低频 `get_market_snapshot` 采集，写入 `realtime_quote_snapshots`；`tick_runs` 同步记录 `quote_snapshots` |
| 港股微观结构 | HK 盘中采集补充 `get_broker_queue` 多档经纪队列、broker metrics 与日级微观结构聚合；US 采集显式关闭 HK-only broker queue |
| 港股状态因子 | 新增 `hk_status` 前向观察因子，使用 snapshot 的 `dark_status` / `sec_status`，默认关闭且权重 0，必须 IC 校准后才可赋权 |
| 观察列表回填 | 每日 watchlist 回填保留 HK `dark_status` / `sec_status` 到 `hk_market_status_snapshots`，用于盘后状态追踪 |
| 自动化 | US/HK tick 采集脚本默认写入 TICKER、quote snapshot、L2 order book、L2 imbalance、dark-pool proxy 和日级微观结构；HK 额外写 broker queue |
| 验证 | `python -m pytest us_strategy\tests hk_strategy\tests tools\tests -q` 通过 271 项；`ruff check .` 通过 |

## 数据持久化

策略状态 SQLite（默认 `us_strategy/positions.db` / `hk_strategy/positions.db`，已 gitignore）：

| 表 | 用途 |
|---|---|
| `positions` | 持仓恢复（含数量、加权成本）|
| `signal_log` | 前向信号日志：`ts / code / last_price / scores(JSON)` |
| `ic_history` | 每日因子 IC：`date / factor / horizon_min / ic / n` |

行情与微观结构 SQLite（默认 `us_strategy/history_data.db`，已 gitignore）：

| 表 | 用途 |
|---|---|
| `history_kline` / `market_snapshot` | 日线/分钟线与盘后快照回填 |
| `hk_market_status_snapshots` | 港股 `dark_status` / `sec_status` 盘后状态快照 |
| `realtime_ticks` / `realtime_quote_snapshots` | 盘中逐笔成交与低频实时快照 |
| `order_book_snapshots` / `order_book_levels` / `order_book_metrics` | L2 多档盘口快照、档位明细与盘口指标 |
| `broker_queue_snapshots` / `broker_queue_levels` / `broker_queue_metrics` | 港股经纪队列快照、档位明细与压力指标 |
| `dark_pool_proxy_events` / `dark_pool_proxy_metrics` | 基于 moomoo 可见逐笔的大额成交代理信号 |
| `l2_imbalance_signals` / `microstructure_alerts` | L2 imbalance 监控信号与告警 |
| `microstructure_daily_features` | 盘中微观结构日级聚合，供后续 IC 校准 |
| `backfill_runs` / `tick_runs` | 历史回填与盘中采集审计记录 |

## 测试

```bash
pytest us_strategy/tests/ -q            # 美股纯逻辑单测，无需 OpenD
pytest hk_strategy/tests/ -q            # 港股纯逻辑单测，无需 OpenD
pytest tools/tests/ -q                  # 数据回填和实时落库工具单测
pytest us_strategy/tests hk_strategy/tests tools/tests -q
pytest --cov=us_strategy --cov-report=term-missing
pytest --cov=hk_strategy --cov-report=term-missing
```

实盘/回测**同源因子引擎**，单测覆盖因子评分、决策逻辑、回测指标、日历/持仓、数据质量、IC 体检等。

## 信号研究 CLI

研究入口只做因子校准、walk-forward、参数搜索和报告输出，不修改实盘配置、权重、watchlist 或数据库。

```bash
python -m research.signal_lab --market us --codes US.AAPL,US.MSFT --start 2025-01-01 --end 2025-12-31 --steps ic,walkforward
python -m research.signal_lab --market hk --codes HK.00700,HK.09988 --start 2025-01-01 --end 2025-12-31 --steps ic,walkforward
```

默认缓存目录为 `data/research_cache`，输出目录为 `report/outputs/signal_research`。可选研究步骤包括 `ic`、`walkforward`、`optuna`、`quantstats`、`vectorbt`；`--refresh-cache` 才会强制重新从 OpenD 拉取数据。

## 关键约定

- **股票代码**：`MARKET.CODE`；美股统一 `US.` 前缀，港股统一 `HK.` 前缀且 5 位补零
- **返回值**：所有 API 返回 `(ret_code, data)`，须先判 `ret_code == RET_OK`
- **评分**：0–100 风险分，有效因子 IC 显著为负
- **稳健默认**：新因子默认关闭、权重 0，须 IC 校准后启用；限价执行默认开启
- **实盘解锁**：实盘交易前必须 `trade_ctx.unlock_trade(password)`，且 `ALLOW_REAL_TRADING=yes`
- **数据可得性**：`get_capital_distribution`、`get_broker_queue` 在不同市场可用性不同，上线前按目标市场先跑 `probe`

## 自动化计划任务

Windows 任务计划程序（不在仓库，启动器在 `us_strategy/*.ps1` / `hk_strategy/*.ps1`）：

| 任务 | 触发 | 作用 |
|---|---|---|
| `MoomooForwardCollect` | 每周一~五 21:00 北京（覆盖 EDT/EST RTH）| 盘中前向采集，写 `signal_log`（需 OpenD）|
| `MoomooICReport` | 每周二~六 06:30 北京（收盘后）| IC 累计体检，写 `ic_history`（只读，无需 OpenD）|
| `MoomooHKForwardCollect` | 每周一~五 09:15 北京（HKT，含午休跳过）| 港股盘中前向采集（需 OpenD）|
| `MoomooHKICReport` | 每周一~五 16:30 北京（HK 收盘后）| 港股 IC 累计体检（只读，无需 OpenD）|
| `MoomooUSDailyWatchlistBackfill` | 每日 06:30 北京（按市场交易日跳过）| US/HK watchlist 盘后历史行情与快照回填 |
| `MoomooUSTickCollect` | 每周一~五 21:00 北京（美股盘前启动）| 美股盘中 TICKER、quote snapshot、L2 order book、imbalance、dark-pool proxy 与日级微观结构落库 |
| `MoomooHKTickCollect` | 每周一~五 09:15 北京（港股盘前启动）| 港股盘中 TICKER、quote snapshot、L2 order book、broker queue、imbalance、dark-pool proxy 与日级微观结构落库 |

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
