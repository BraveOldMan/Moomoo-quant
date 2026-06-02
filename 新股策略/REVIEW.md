# 新股 IPO 策略 — 检查与升级存档

> 日期：2026-06-03 · 范围：`新股策略/` 全量重构升级
> 验证：`py_compile` 全通过 · `pytest` 55 项全绿 · 全模块导入正常

本文档归档一次完整的策略代码检查（review）与升级（upgrade）结论，作为后续迭代的基线参考。

---

## 一、总体评价

原实现分层清晰（config / signals / strategy / trader / monitor / persistence / alerts / backtest / market_calendar），风控要素齐全，工程质量中上。

**核心隐忧**：策略有效性高度依赖两个数据字段——`turnover_rate`（换手率）与 `get_capital_distribution`（机构资金分布，原权重最高 0.55）。这两个接口在 **moomoo 美股**上的可得性/质量存疑（资金分布、经纪队列类接口主要面向港股/A 股）。若美股返回空，旧逻辑会 `return None` 整只跳过 → 策略实际永不产生信号。

> ⚠️ **上线前第一优先级**：运行 `python -m 新股策略.probe US.XXX` 实测核心因子可得性。

---

## 二、已修复的缺陷（按严重度）

### CRITICAL
1. **回测与实盘信号不同源** —— 旧回测用 `main_in_flow`、权重 0.4/0.6，且无止损/熔断/PDT/锁定期/流动性/动量，回测结论对实盘无指导意义。
   **修复**：抽出 `features.py` 纯函数评分，回测与实盘共用同一套因子与权重。
2. **市价单无滑点保护**（`place_order price=0 + MARKET`）—— 新股价差大、深度薄，滑点吞噬收益。
   **修复**：`trader` 改 marketable-limit（现价 ± `limit_price_tolerance_pct`）+ 下单后轮询订单状态确认成交。
3. **熔断基准错误** —— 旧逻辑取当天第一次调用时的净值；盘中启动则基准失真。
   **修复**：支持 `prev_close / day_open / first_seen`，`main` 在开盘首个 tick 注入基准净值。

### HIGH
4. **成本基准只记第一批** —— 分批建仓后止损基于第一批价。
   **修复**：`strategy` 维护加权平均成本（total_qty / total_cost）。
5. **`max_positions` 误伤加仓** —— `count_open_positions` 把已持仓计入，满仓后无法加仓。
   **修复**：`buy(is_new_position=...)` 区分新开仓 / 加仓。
6. **并发竞态** —— 推送线程与轮询线程同时调用 `on_quote`，策略状态无锁。
   **修复**：`main` 改单线程事件队列消费；策略状态额外加 `RLock`。
7. **伪 PDT（自然日）** —— `(today - buy_date).days` 周末也计数。
   **修复**：`_trading_days_between` 复用 NYSE 日历按交易日计算。

### MEDIUM
8. **API 撞频风险** —— 每股每次 4 个请求 × 轮询+推送。
   **修复**：`data_access.py` TTL 缓存 + 令牌桶限流。
9. **`position_list_query` 重复查询** —— 修复：单次查询走缓存复用。
10. **回测无成本/基准/样本外** —— 修复：佣金+滑点模型、SPY 基准对比、walk-forward。
11. **无单元测试** —— 修复：新增 `tests/`，55 项覆盖纯逻辑。

---

## 三、新增专业分析方法

| 能力 | 模块 | 说明 |
|---|---|---|
| ATR / ORB / RS / VWAP 因子 | `features.py` | 新股首日无均线，ORB 比 MA 更适用；RS 对标 SPY；VWAP 偏离衡量多空掌控 |
| 缺失因子自动归一化 | `features.score_from_features` | 数据不可用的因子自动剔除并重新归一化，保障美股可用性 |
| 因子 IC / IR | `analysis.py` | Spearman 秩相关（无 scipy 依赖），校准权重取代拍脑袋 |
| 分层回测（quantile） | `analysis.py` | 检验因子分位组未来收益单调性 |
| 锁定期事件研究（CAR） | `analysis.py` | 到期日前后累计异常收益（个股 − SPY） |
| 成本模型 | `backtest.py` | 佣金（每股+最低）+ 滑点（bps），买卖均扣 |
| 风险指标 | `backtest.py` | 年化、Sharpe、Sortino、Calmar、最大回撤、Alpha |
| walk-forward | `backtest.py` | 时间等分样本外检验，抑制过拟合 |
| ATR 波动率仓位 | `features.atr_position_size` | 按单笔风险预算定 size，替代固定 `position_ratio` |
| 数据可得性探针 | `probe.py` | 上线前实测美股各接口字段 |

### 评分约定
所有 `*_score` 返回 **0–100 风险分**：`0`=低风险/偏多，`100`=高风险/偏空。
因此**有效因子的 IC 应显著为负**（风险分越高，未来收益越低），`|IC|>0.03`、`|IR|>0.5` 视为有意义。

---

## 四、稳健默认与开关

- **新因子默认关闭**（`USE_RS / USE_ORB / USE_VWAP_SIGNAL = False`）：须先用 `FactorAnalyzer.factor_ic()` 校准后再启用。
- **限价执行默认开启**（`USE_LIMIT_ORDERS = True`）。
- **ATR 仓位默认关闭**（`USE_ATR_SIZING = False`），保留原 `position_ratio` 行为。
- 全部可通过环境变量切换，见 `main.py` 顶部 docstring。

---

## 五、上线前 checklist

1. `python -m 新股策略.probe US.RDDT US.ARM` —— 确认核心因子可得性。
2. `FactorAnalyzer.factor_ic(codes, start, end)` —— 校准因子，剔除 IC 不显著者。
3. `BacktestEngine.run_walk_forward(...)` —— 样本外验证 Sharpe / Calmar / Alpha。
4. 据回测结果调整 `config.py` 权重与阈值，再启用相应因子开关。
5. 先 `SIMULATE` 跑通全流程，再切 `REAL`（需 `TRADE_PASSWORD`）。

---

## 六、模块清单

| 模块 | 职责 |
|---|---|
| `config.py` | 全部参数（含执行/波动率仓位/可配权重/缓存限流/熔断基准/新因子/回测成本）+ `active_weights()` |
| `data_access.py` | TTL 缓存 + 令牌桶限流的行情/交易数据门面 |
| `features.py` | 统一特征与纯函数评分 + 技术指标（ATR/VWAP）+ ATR 仓位 |
| `signals.py` | 经 `data_access` 取数、调 `features` 评分，缺失因子降级 |
| `strategy.py` | 决策核心：加权成本、交易日 PDT、熔断基准、加锁 |
| `trader.py` | marketable-limit 执行 + 成交轮询 + 新开仓/加仓区分 |
| `backtest.py` | 同源回测 + 成本/基准/风险指标 + walk-forward |
| `analysis.py` | 因子 IC/IR、分层回测、锁定期事件研究 |
| `main.py` | 单线程事件队列编排 |
| `probe.py` | 数据可得性探针 |
| `persistence.py` | SQLite 持仓恢复（含 qty） |
| `market_calendar.py` | NYSE 假日表 |
| `monitor.py` / `alerts.py` | 实时行情订阅 / 多渠道告警 |
| `tests/` | 55 项纯逻辑单测 |
