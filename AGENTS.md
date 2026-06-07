# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 项目概述

本项目是基于 **moomoo API**（原 Futu API）的 Python 量化交易策略开发目录，覆盖**美股（US）**与**港股（HK）**两套程序化交易策略，使用 moomoo OpenD 网关客户端进行行情获取和交易执行。

> **市场范围**：`us_strategy/` 面向美股，股票代码前缀统一 `US.`，例如 `US.AAPL`、`US.TSLA`；`hk_strategy/` 面向港股，股票代码前缀统一 `HK.` 且 5 位补零，例如 `HK.00700`、`HK.09988`。本仓库不涉及 A 股（沪深）。

SDK 源码位于 `MMAPI4Python_10.7.6708/`，版本为 10.7.6708。

## 安装与运行前提

**必须先启动 moomoo OpenD 网关**，API 才能连接：

- 默认连接地址：`host='127.0.0.1', port=11111`
- OpenD 下载：https://openapi.moomoo.com/moomoo-api-doc/en/quick/opend-base.html

安装 SDK：

```bash
pip install moomoo-api
# 或从本地 SDK 目录安装
pip install -e MMAPI4Python_10.7.6708/
```

核心依赖：`pandas`, `simplejson`, `protobuf>=3.20.0`, `PyCryptodome`  
策略依赖（可选）：`talib`（用于技术指标计算）

## 常用命令

```bash
# 美股数据可得性探针（需先启动 OpenD）
python -m us_strategy.probe US.RDDT US.ARM

# 港股数据可得性探针（需先启动 OpenD）
python -m hk_strategy.probe HK.00700 HK.09988

# 运行策略（模拟盘默认，仍需 OpenD）
python -m us_strategy.main
python -m hk_strategy.main

# 运行测试（无需 OpenD）
pytest us_strategy/tests/ -q
pytest hk_strategy/tests/ -q

# 代码检查（仅在依赖已安装时运行）
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

**股票代码格式**：`MARKET.CODE`。美股统一 `US.` 前缀，例如 `US.AAPL`、`US.TSLA`、`US.NVDA`；港股统一 `HK.` 前缀并 5 位补零，例如 `HK.00700`、`HK.09988`。

**返回值约定**：所有 API 调用返回 `(ret_code, data)` 元组；必须检查 `ret_code == RET_OK` 再使用 `data`，否则 `data` 是错误信息字符串。

**接口限频配置**：仓库统一使用 `moomoo_rate_limits.py` 记录 moomoo 官方/保守限频；不要在脚本里重新硬编码请求间隔。高频默认：`get_market_snapshot` 60 次/30 秒（0.5 秒间隔）、`request_history_kline` 首页 60 次/30 秒、`get_capital_flow` / `get_capital_distribution` 30 次/30 秒、`get_option_chain` 10 次/30 秒（3 秒间隔）、交易查询类 `position_list_query` / `accinfo_query` / `order_list_query` 10 次/30 秒/账户且仅 `refresh_cache=True` 时受限。订阅缓存类 `get_stock_quote` / `get_order_book` / `get_rt_ticker` / `get_rt_data` / `get_cur_kline` / `get_broker_queue` 不按服务器请求限频计算，但受订阅额度和行情权限约束。

**中文编码规则**：所有包含中文的文本文件、Markdown、Python 源码、配置文件、报告模板和日志解析代码，默认使用 UTF-8 编码读写。Python 文件读写必须显式传入 `encoding="utf-8"`；读取可能带 BOM 的外部文件时使用 `utf-8-sig`。PowerShell 查看中文文件时使用 `Get-Content -Encoding UTF8`。不得用系统默认编码、GBK 或无编码参数隐式读写中文内容。CSV 若主要给 Excel 打开，可使用 `utf-8-sig`；二进制文件、SDK 生成文件和第三方源码不做强制重编码。

**PowerShell 边界**：`.ps1` 仅允许作为计划任务或本地命令启动器，不承载数据处理、策略判断、飞书卡片生成或 Markdown 拼装。新增自动化优先实现为 `tools/*.py` 或 `python -m ...` CLI，PowerShell 只包一层 `Set-Location`、少量环境变量、前置条件检查和启动命令。需要日志重定向时沿用当前已验证模式：设置 `PYTHONUTF8=1`，用 `cmd /c "... >> log 2>&1"` 追加原生日志，避免 Windows PowerShell 5.1 写 UTF-16 或把 native stderr 包装成 `NativeCommandError`。OpenD 探测、fail-closed 门禁、飞书发送回读、JSON/Markdown 生成与解析必须放在 Python 内，不放在 PowerShell 管道、here-string 或多层引号中。

**加密连接**（可选）：
```python
SysConfig.enable_proto_encrypt(True)
SysConfig.set_init_rsa_file("conn_key.txt")  # RSA 1024位 PKCS#1 私钥
```

**调试日志**：
```python
set_futu_debug_model(True)  # 启用详细日志，输出至 %APPDATA%\com.moomoonn.FutuOpenD\Log
```

**真实交易解锁**：实盘交易前必须调用 `trade_ctx.unlock_trade(password)`，并且策略入口必须满足 `TRADE_ENV=REAL` 与 `ALLOW_REAL_TRADING=yes` 双确认。

## 飞书日报发送规则

本仓库的日报/研究报告发送必须采用“云文档全文 + 群卡片摘要”链路，禁止再把完整 Markdown 直接作为群消息发送：

1. 先创建全文云文档：

```powershell
lark-cli markdown +create --file report\outputs\...\summary.md --name 美股日报_YYYYMMDD.md
```

2. 再发送飞书 `interactive` 卡片。请求参数与请求体必须写入文件后通过 `@file` 传参，避免 PowerShell 多行文本、中文和 JSON 转义导致只显示标题或正文截断：

```powershell
lark-cli api POST /open-apis/im/v1/messages --params @lark_send_params.json --data @lark_send_body.json
```

其中 `lark_send_params.json` 固定包含：

```json
{"receive_id_type": "chat_id"}
```

`lark_send_body.json` 必须包含：

```json
{
  "receive_id": "oc_xxx",
  "msg_type": "interactive",
  "content": "<飞书卡片 JSON 字符串>"
}
```

卡片必须包含报告标题、30 秒结论/摘要、以及打开完整云文档的按钮或链接。不要使用 `lark-cli im +messages-send --markdown`、`--markdown <summary.md>`、多行 `--text` 或把整篇 Markdown 放入群消息正文；这些方式在 Windows + lark-cli 下已出现“只发一个标题”的错误。

发送后必须同时保存并回读：

- `lark_create.json`
- `lark_card.json`
- `lark_send_params.json`
- `lark_send_body.json`
- `lark_send.json`
- `lark_fetch.json`
- `lark_message.json`

验收标准是 `msg_type=interactive`、标题、摘要关键字段、云文档链接和 `message_id` 均可从线上回读确认。`receipt=sent`、命令退出码为 0 或本地文件存在都不能单独视为发送成功。

修正已发送消息时，只有已发送的 `interactive` 卡片可以用 `PATCH /open-apis/im/v1/messages/{message_id}` 原地更新；`text/post` 消息不能原地变成卡片，只能按原类型编辑或重新发送新的卡片消息。

## 策略开发参考

`examples/macd_strategy.py` 是典型策略模板，展示了：
- Context 初始化与关闭
- 历史 K 线请求（`request_history_kline`）
- 仓位查询（`position_list_query`）
- 账户资金查询（`accinfo_query`）
- 下买/卖单（`place_order`）

模拟环境使用 `trd_env=ft.TrdEnv.SIMULATE`，实盘使用 `ft.TrdEnv.REAL`。

## 美股量化策略（`us_strategy/`）

`us_strategy/` 面向美股。包名保留历史命名，但当前已不限于新股：运行时 universe = IPO 扫描 ∪ `WATCHLIST` 自选清单 ∪ 现有持仓。完整检查与升级记录见 `us_strategy/REVIEW.md`。

### 模块架构

```
main.py          单线程事件队列编排（推送+轮询统一投递，串行消费，无并发下单竞态）
  ├─ data_access.py   TTL 缓存 + 令牌桶限流的行情/交易数据门面（防撞频，单查复用）
  ├─ signals.py       经 data_access 取数 → 调 features 评分；缺失因子自动降级
  │    └─ features.py 统一特征与纯函数评分（实盘/回测共用，杜绝"测的不是跑的"）
  ├─ strategy.py      决策核心：加权成本、交易日 PDT、熔断基准锚定、RLock 加锁
  ├─ trader.py        marketable-limit 限价执行 + 成交轮询 + 新开仓/加仓区分
  ├─ persistence.py   SQLite 持仓恢复（含 qty，支持旧库迁移）
  ├─ market_calendar.py / clock.py  NYSE 假日表 + 纽约市场日工具
  └─ alerts.py / monitor.py  多渠道告警 / 实时行情订阅

backtest.py      同源回测 + 佣金/滑点成本 + SPY 基准/Alpha + Sharpe/Sortino/Calmar + walk-forward
analysis.py      因子 IC/IR、分层回测、锁定期事件研究（CAR）+ forward_ic_from_log
probe.py         数据可得性探针（上线前实测美股各接口字段）
tests/           当前 pytest 收集 103 项纯逻辑单测
```

### 关键约定（本策略包）

- **评分约定**：所有 `*_score` 返回 0–100 **风险分**（0=低风险/偏多，100=高风险/偏空）；
  有效因子的 IC 应显著为负。
- **因子权重**：用 `config.active_weights()` 输出当前启用因子；`features.score_from_features`
  对数据缺失的因子自动剔除并归一化。
- **稳健默认**：新因子（RS/ORB/VWAP、microstructure、short、option_iv）默认关闭、权重 0，须先用 `analysis.FactorAnalyzer.factor_ic()` 或 `forward_ic_from_log()` 校准后再启用；限价执行默认开启；ATR 仓位默认关闭。全部经环境变量切换（见 `main.py` docstring）。
- **universe 默认**：`WATCHLIST` 环境变量优先；未设置时读取 `us_strategy/watchlist.txt`，该文件为空或不存在时才仅 IPO 扫描。
- **数据可得性风险**：`get_capital_distribution`、`get_broker_queue` 在美股可能不可用——
  上线前必须先跑 `python -m us_strategy.probe US.XXX` 确认核心因子落地。

### 常用命令

```bash
python -m us_strategy.probe US.RDDT US.ARM   # 数据可得性探针（需 OpenD）
python -m us_strategy.main                    # 运行实盘/模拟策略（需 OpenD）
pytest us_strategy/tests/ -q                  # 当前收集 103 项单测（无需 OpenD）
```

## 港股量化策略（`hk_strategy/`）

`hk_strategy/` 是港股（HKEX）平行策略包，复用同一套因子、决策、执行、回测与校准框架，仅在市场口径上特化。

### 港股特化点

| 维度 | `us_strategy` | `hk_strategy` |
|---|---|---|
| 代码前缀 | `US.` | `HK.`，5 位补零 |
| 时区 | America/New_York | Asia/Hong_Kong |
| 交易时段 | 09:30-16:00 连续 | 09:30-12:00 / 13:00-16:00，午休闭市 |
| 交易日历 | NYSE 规则日历 | HKEX API 刷新优先，硬编码假日表兜底 |
| PDT | 默认 `MIN_HOLD_DAYS=1` | 港股无 PDT，默认 `MIN_HOLD_DAYS=0` |
| 成本模型 | 每股佣金 | 成交额百分比 + 印花税 + 交易所费用 |
| 回测基准 | `US.SPY` | `HK.800000`（恒指） |
| 默认 DB | `us_strategy/positions.db` | `hk_strategy/positions.db` |

### 常用命令

```bash
python -m hk_strategy.probe HK.00700 HK.09988   # 数据可得性探针（需 OpenD）
python -m hk_strategy.main                        # 运行实盘/模拟策略（需 OpenD）
pytest hk_strategy/tests/ -q                       # 当前收集 113 项单测（无需 OpenD）
```

港股扩展因子必须在港股样本上重新做 IC 校准，不得沿用美股结论。
