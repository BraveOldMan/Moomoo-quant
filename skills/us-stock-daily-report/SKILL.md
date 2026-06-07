---
name: us-stock-daily-report
description: Use this repository-local skill to run the Moomoo US stock daily report after the US close. It analyzes US index moves, the us_strategy watchlist, main strategy factor signals, limited-depth option chains, and sends a Feishu interactive card summary with a cloud document link under fail-closed validation.
---

# 美股日报

## 固定入口

在 `D:\Moomoo-quant` 下运行：

```powershell
python -m tools.run_us_stock_daily_report --chat-id oc_bc9a36b4392dbe632fb4e50a3ef7ef17 --send
```

只读烟测：

```powershell
python -m tools.run_us_stock_daily_report --date 2026-06-05 --no-send --output-dir .codex_tmp/us_daily_smoke
```

飞书 dry-run：

```powershell
python -m tools.run_us_stock_daily_report --send --dry-run-lark --chat-id oc_bc9a36b4392dbe632fb4e50a3ef7ef17
```

## 工作流

- 必须先确认 OpenD 在线；仅允许使用 `OpenQuoteContext`，不得创建交易上下文、解锁交易或下单。
- 自动推断纽约市场交易日；北京时间周二到周六 05:30 运行时，应映射到上一美股交易日。若未到美股收盘后 30 分钟、目标日非交易日或关键日线未落地，fail closed。
- 默认读取 `us_strategy/watchlist.txt`，忽略注释、空行和非 `US.` 代码。
- 主策略信号必须复用 `StrategyConfig`、`SignalCalculator`、`IPOStrategy.evaluate()`；不得改权重、阈值、watchlist、数据库或实盘主链路。
- 期权分析使用限深口径：每只观察股最近 2 个到期日，各取 ATM call/put；输出 IV、skew、PCR、OI 与风险标签。无期权或缺字段必须显式标注。

## 输出与验收

输出目录为 `report/outputs/us_daily/YYYYMMDD/`，至少包含：

- `summary.md`
- `report.json`
- `stock_factors.csv`
- `options.csv`
- 飞书发送时还必须包含 `lark_create.json`、`lark_card.json`、`lark_send_body.json`、`lark_send_params.json`、`lark_send.json`、`lark_fetch.json`、`lark_message.json`

飞书发送使用全局 `lark-cli` profile，不在仓库写入 token。发送链路固定为：先 `markdown +create` 生成全文云文档，再用 `api POST /open-apis/im/v1/messages` 发送 `interactive` 卡片摘要，卡片必须包含 30 秒结论和打开完整云文档的按钮链接。发送后必须执行文档回读和消息回读，并校验消息为卡片、日期标题和云文档链接；不能只凭 `receipt=sent` 或命令退出码判定成功。

禁止使用以下旧发送方式：

- `lark-cli im +messages-send --markdown ...`
- `lark-cli im +messages-send --text ...` 发送多行长正文
- 把整篇 `summary.md` 直接塞进群消息正文
- 只保存 `lark_send.json`，不做文档和消息双回读

正确群消息方式固定为：

```powershell
lark-cli api POST /open-apis/im/v1/messages --params @lark_send_params.json --data @lark_send_body.json
```

其中 `lark_send_body.json` 必须是 `msg_type=interactive` 的卡片发送体；全文内容只放飞书云文档，群消息只放摘要和云文档入口。

若需要修正已发送消息：已发送卡片可用 `PATCH /open-apis/im/v1/messages/{message_id}` 原地更新；`text/post` 消息不能原地变成卡片，只能按原类型编辑或重新发送新的卡片消息。

## 报告边界

- 报告是观察和复盘，不输出实盘交易指令。
- 数据缺口、OpenD 不可用、飞书失败、报告为空或日期不匹配时，保留本地失败产物并停止发送。
- 最终回复必须给出本地报告路径、飞书文档链接、群消息 `message_id` 和回执文件路径。
