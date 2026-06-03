# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

本项目是基于 **moomoo API**（原 Futu API）的 Python 量化交易策略开发目录，专注于**美股（US）市场**的程序化交易，使用 moomoo OpenD 网关客户端进行行情获取和交易执行。

> **市场范围**：本仓库所有策略均以美股为目标市场，不涉及 A 股（沪深）。股票代码前缀统一使用 `US.`，例如 `US.AAPL`、`US.TSLA`。

SDK 源码位于 `MMAPI4Python_10.6.6608/`，版本为 10.6.6608。

## 安装与运行前提

**必须先启动 moomoo OpenD 网关**，API 才能连接：

- 默认连接地址：`host='127.0.0.1', port=11111`
- OpenD 下载：https://openapi.moomoo.com/moomoo-api-doc/en/quick/opend-base.html

安装 SDK：

```bash
pip install moomoo-api
# 或从本地 SDK 目录安装
pip install -e MMAPI4Python_10.6.6608/
```

核心依赖：`pandas`, `simplejson`, `protobuf>=3.20.0`, `PyCryptodome`  
策略依赖（可选）：`talib`（用于技术指标计算）

## 常用命令

```bash
# 运行示例策略（需先启动 OpenD）
python MMAPI4Python_10.6.6608/moomoo/examples/macd_strategy.py

# 运行行情推送示例
python MMAPI4Python_10.6.6608/moomoo/examples/quote_push.py

# 运行测试（如有）
pytest --cov=moomoo --cov-report=term-missing

# 代码格式化
black .
ruff check .
```

## 架构概述

### 通信架构

所有 API 调用都通过本地 OpenD 网关中转，不直接连接交易所：

```
策略代码 → moomoo Python SDK → TCP(11111) → OpenD 网关 → 交易所/行情源
```

### 两大核心 Context 类

| 类 | 用途 |
|---|---|
| `OpenQuoteContext` | 行情订阅、历史K线、市场快照、筛股 |
| `OpenSecTradeContext` | 下单、查仓位、查账户、查历史成交 |

两者均继承自 `OpenContextBase`（`common/open_context_base.py`），管理 TCP 连接、心跳保活、断线重连、同步/异步请求。

### 数据流模式

**同步查询**：调用后阻塞等待，返回 `(ret_code, DataFrame)`  
**异步推送**：订阅后通过回调接收实时数据，需继承 `*HandlerBase` 并重写 `on_recv_rsp()`

```python
# 同步查询示例
ret, df = quote_ctx.get_market_snapshot(['HK.00700'])
if ret == RET_OK:
    print(df)

# 异步推送示例
class MyQuoteHandler(StockQuoteHandlerBase):
    def on_recv_rsp(self, rsp_pb):
        ret, content = super().on_recv_rsp(rsp_pb)
        if ret == RET_OK:
            # 处理推送数据
            pass
        return ret, content

quote_ctx.set_handler(MyQuoteHandler())
quote_ctx.start()
quote_ctx.subscribe(['HK.00700'], [SubType.QUOTE])
```

### 协议层（common/pb/）

Protobuf 生成文件（`*_pb2.py`），不要手动编辑。支持两种通信格式：
- `ProtoFMT.Protobuf`（默认，性能更好）
- `ProtoFMT.Json`（调试方便）

通过 `SysConfig.set_proto_fmt()` 切换。

## 关键约定

**股票代码格式**：`MARKET.CODE`，本仓库统一使用 `US.` 前缀，例如 `US.AAPL`、`US.TSLA`、`US.NVDA`

**返回值约定**：所有 API 调用返回 `(ret_code, data)` 元组；必须检查 `ret_code == RET_OK` 再使用 `data`，否则 `data` 是错误信息字符串。

**加密连接**（可选）：
```python
SysConfig.enable_proto_encrypt(True)
SysConfig.set_init_rsa_file("conn_key.txt")  # RSA 1024位 PKCS#1 私钥
```

**调试日志**：
```python
set_futu_debug_model(True)  # 启用详细日志，输出至 %APPDATA%\com.moomoonn.FutuOpenD\Log
```

**真实交易解锁**：美股实盘交易前必须调用 `trade_ctx.unlock_trade(password)`

## 策略开发参考

`examples/macd_strategy.py` 是典型策略模板，展示了：
- Context 初始化与关闭
- 历史 K 线请求（`request_history_kline`）
- 仓位查询（`position_list_query`）
- 账户资金查询（`accinfo_query`）
- 下买/卖单（`place_order`）

模拟环境使用 `trd_env=ft.TrdEnv.SIMULATE`，实盘使用 `ft.TrdEnv.REAL`。

## 美股量化策略（`新股策略/`）

> 包名仍为「新股策略」（保留以零迁移风险），但自 v1.2.0 起已**不限于新股**：
> 既能自动扫描 IPO，也能通过自选清单（`WATCHLIST`）分析任意美股，同一套因子引擎共用。

针对美股的实盘策略包，已完成系统性升级。完整的检查与升级记录见 `新股策略/REVIEW.md`（含 v1.1/v1.2 增量）。

### 模块架构

```
main.py          单线程事件队列编排（推送+轮询统一投递，串行消费，无并发下单竞态）
                 universe = IPO 扫描 ∪ 自选清单(WATCHLIST) ∪ 现有持仓
  ├─ data_access.py   TTL 缓存 + 令牌桶限流的行情/交易数据门面（防撞频，单查复用）
  │                   含微观结构(rt_ticker/order_book)、做空(short)、期权链封装
  ├─ signals.py       经 data_access 取数 → 调 features 评分；缺失因子自动降级
  │    │              换手率阈值按标的自动分 IPO/成熟股 profile
  │    └─ features.py 统一特征与纯函数评分（实盘/回测共用，杜绝"测的不是跑的"）
  ├─ strategy.py      决策核心：加权成本、交易日 PDT、熔断基准锚定、RLock 加锁
  ├─ trader.py        marketable-limit 限价执行 + 成交轮询 + 新开仓/加仓区分
  ├─ persistence.py   SQLite 持仓恢复（含 qty）+ SignalLogStore 前向日志(signal_log)
  ├─ market_calendar.py  NYSE 假日表
  └─ alerts.py / monitor.py  多渠道告警 / 实时行情订阅(QUOTE + 可选 TICKER/ORDER_BOOK)

backtest.py      同源回测 + 佣金/滑点成本 + SPY 基准/Alpha + Sharpe/Sortino/Calmar + walk-forward
analysis.py      因子 IC/IR、分层回测、锁定期事件研究（CAR）+ forward_ic_from_log（微观因子前向校准）
probe.py         数据可得性探针（上线前实测美股各接口字段，含微观/做空/期权扩展因子）
tests/           68 项纯逻辑单测
```

### 因子总览（评分均为 0–100 风险分；新增因子默认关闭、权重 0）

| 组别 | 因子（`scores` 键） | 数据源 |
|---|---|---|
| 核心 | turnover / capital / momentum | snapshot / capital_distribution |
| 技术 | orb / rs / vwap / ATR 仓位 | history_kline |
| 盘中微观结构 | order_flow(CVD) / obi / intraday_flow | rt_ticker / order_book / capital_flow INTRADAY |
| 做空面 | short | short_interest / daily_short_volume |
| 期权隐含 | option_iv（IV skew + PCR） | option_chain + 期权 snapshot |

> 微观结构因子无历史回放 → 用 `SignalLogStore` 前向日志 + `analysis.forward_ic_from_log` 校准。

### 关键约定（本策略包）

- **评分约定**：所有 `*_score` 返回 0–100 **风险分**（0=低风险/偏多，100=高风险/偏空）；
  有效因子的 IC 应显著为负。
- **因子权重**：用 `config.active_weights()` 输出当前启用因子；`features.score_from_features`
  对数据缺失的因子自动剔除并归一化。
- **稳健默认**：所有新因子（RS/ORB/VWAP、microstructure、short、option_iv）默认关闭、权重 0，
  须先用 `analysis.FactorAnalyzer.factor_ic()`（有历史的因子）或 `forward_ic_from_log()`
  （微观因子）校准后再启用；限价执行默认开启；ATR 仓位默认关闭。全部经环境变量切换（见 `main.py` docstring）。
- **universe 通用化**：`WATCHLIST=US.AAPL,US.TSLA` 把任意美股纳入分析（默认空=仅 IPO 扫描）。
  观察列表来源优先级：`WATCHLIST` 环境变量 > `新股策略/watchlist.txt`（每行一代码、`#` 注释、`WATCHLIST_FILE` 可改路径）> 空。
  自选标的无上市日 → 锁定期因子 no-op、换手率走成熟股 profile（阈值 `5/15`，低于 IPO 的 `80/150`）。
- **数据可得性风险**：`get_capital_distribution`、`get_broker_queue` 在美股可能不可用——
  上线前必须先跑 `python -m 新股策略.probe US.XXX` 确认核心因子落地。

### 常用命令

```bash
python -m 新股策略.probe US.RDDT US.ARM   # 数据可得性探针（需 OpenD，含扩展因子）
python -m 新股策略.main                    # 自动加载 watchlist.txt（含 MRVL+实盘持仓）+ IPO 扫描
WATCHLIST=US.AAPL,US.TSLA python -m 新股策略.main  # 环境变量临时覆盖文件（需 OpenD）
pytest 新股策略/tests/ -q                  # 运行 73 项单测（无需 OpenD）
```
