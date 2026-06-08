# Moomoo Quant

> Current version: `v1.9.0` (2026-06-09)

Moomoo Quant is a dual-market quantitative trading and research framework built on top of the
moomoo OpenD gateway, formerly known as the Futu OpenAPI gateway. It covers both US equities and
Hong Kong equities, with shared strategy architecture and market-specific execution rules.

The project is designed as a full research-to-simulation loop:

```text
market data -> factor scoring -> decision engine -> simulated execution
            -> persistence -> forward IC calibration -> research reports
```

This repository is intended for research, simulation, data collection, and engineering reference.
It is not investment advice and is not a turnkey live-trading system.

## Scope

Supported markets:

| Market | Package | Code format | Examples |
| --- | --- | --- | --- |
| US equities | `us_strategy/` | `US.SYMBOL` | `US.AAPL`, `US.TSLA`, `US.NVDA` |
| Hong Kong equities | `hk_strategy/` | `HK.00000` | `HK.00700`, `HK.09988`, `HK.03690` |

This project does not cover A-shares, futures execution, cryptocurrency execution, or wallet-based
trading.

The repository includes:

- Strategy code for US and HK simulated trading.
- Shared research, backtesting, IC diagnostics, and reporting tools.
- Forward signal collection and scheduled-task validation tools.
- Microstructure feature collection helpers.
- Local development instructions and safety rules.

The repository intentionally excludes:

- Live account credentials and trading passwords.
- Local SQLite databases.
- OpenD runtime logs.
- Generated backtest reports and charts.
- SDK vendor directories such as `MMAPI4Python_*`.
- Any proprietary data export that should not be redistributed.

## Safety First

The default runtime environment is simulation:

```text
TRADE_ENV=SIMULATE
```

Live trading is guarded by three independent requirements:

1. `TRADE_ENV=REAL`
2. `ALLOW_REAL_TRADING=yes`
3. `TRADE_PASSWORD` must be set so the moomoo trade context can be unlocked.

The simulation launcher scripts explicitly set `TRADE_ENV=SIMULATE` and remove live-trading unlock
variables. Do not bypass this behavior unless you have reviewed the code, OpenD account state,
broker permissions, market data permissions, and all order-size controls.

Expected buy failures caused by insufficient cash or budget are treated as log-only events to avoid
alert spam. They are not silently converted into successful orders.

## Architecture

All broker and market-data calls go through the local OpenD gateway:

```text
strategy code -> moomoo Python SDK -> TCP 127.0.0.1:11111 -> OpenD -> market data / broker API
```

The two core SDK contexts are:

| SDK context | Purpose |
| --- | --- |
| `OpenQuoteContext` | Quotes, historical candles, market snapshots, order book, ticker, broker queue, screeners |
| `OpenSecTradeContext` | Orders, account info, positions, fills, order history |

Every SDK call returns a `(ret_code, data)` tuple. Code must check `ret_code == RET_OK` before
using `data`; on failure, `data` is usually an error string.

### Package Layout

```text
.
|-- us_strategy/              # US equity strategy package
|-- hk_strategy/              # Hong Kong equity strategy package
|-- research/                 # IC diagnostics, walk-forward research, backtest reports
|-- tools/                    # Data collection, health checks, task validators, utilities
|-- skills/                   # Codex local skill metadata for project workflows
|-- moomoo_rate_limits.py     # Conservative moomoo API rate-limit facts
|-- dark_pool_proxy.py        # Large-print proxy tracker based on visible moomoo ticks
|-- order_book_l2.py          # L2 order-book imbalance and pressure utilities
|-- ipo_runtime.py            # Today-IPO runtime helpers
|-- ipo_watchlist.py          # IPO watchlist persistence helpers
|-- AGENTS.md                 # Repository-specific development rules
|-- CLAUDE.md                 # Additional local development notes
`-- VERSION                   # Current project version
```

### Strategy Package Layout

Both `us_strategy/` and `hk_strategy/` follow the same shape:

| Module | Responsibility |
| --- | --- |
| `main.py` | Runtime entrypoint. Single-threaded event consumption for decisions and order submission |
| `config.py` | Environment-driven `StrategyConfig`, feature switches, weights, thresholds |
| `data_access.py` | TTL cache and token-bucket facade for quote/trade data |
| `features.py` | Pure factor scoring functions shared by live/sim and backtest logic |
| `signals.py` | Data retrieval, score composition, `SignalResult`, universe profiling |
| `strategy.py` | Decision engine: weighted cost, PDT/min-hold rules, circuit breaker, stop logic |
| `trader.py` | Marketable-limit execution, fill polling, add/open distinction |
| `persistence.py` | SQLite position recovery and forward signal logging |
| `monitor.py` | Realtime quote subscription |
| `alerts.py` | Email, Telegram, and Feishu/Lark alert adapters |
| `market_calendar.py` | Market holiday and trading-day logic |
| `backtest.py` | Source-aligned backtest engine with cost model and risk metrics |
| `analysis.py` | IC/IR, quantile tests, event studies, forward IC from logs |
| `probe.py` | Data availability probe before running strategies |
| `forward_monitor.py` | Forward scoring logger. It does not place orders |
| `ic_report.py` | Daily forward IC health report |

## Market Differences

| Dimension | US strategy | HK strategy |
| --- | --- | --- |
| Package | `us_strategy` | `hk_strategy` |
| Code prefix | `US.` | `HK.` with five-digit code |
| Time zone | `America/New_York` | `Asia/Hong_Kong` |
| Regular session | 09:30-16:00 continuous | 09:30-12:00 and 13:00-16:00 |
| Lunch break | No | Yes |
| Calendar | NYSE calendar | HKEX API first, hard-coded holiday fallback |
| Minimum holding | PDT-aware, default `MIN_HOLD_DAYS=1` | No PDT, default `MIN_HOLD_DAYS=0` |
| Cost model | Per-share commission | Turnover percentage, stamp duty, exchange fees |
| Benchmark | `US.SPY` | `HK.800000` |
| Lot size | 1 share | Broker lot size is queried and respected |
| Default position DB | `us_strategy/positions.db` | `hk_strategy/positions.db` |

HK-specific factors must be calibrated on HK samples. Do not reuse US IC conclusions directly.

## Installation

### 1. Install moomoo OpenD

OpenD must be running before any SDK calls can connect.

- Default host: `127.0.0.1`
- Default port: `11111`
- OpenD documentation: <https://openapi.moomoo.com/moomoo-api-doc/en/quick/opend-base.html>

### 2. Create a Python environment

```powershell
git clone https://github.com/BraveOldMan/moomoo-quant.git
Set-Location .\moomoo-quant

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
```

### 3. Install dependencies

Install the official moomoo API package:

```powershell
python -m pip install moomoo-api
```

If you have a local SDK checkout, you can install it instead:

```powershell
python -m pip install -e .\MMAPI4Python_10.7.6708
```

Core runtime dependencies used by the repository include:

- `pandas`
- `simplejson`
- `protobuf>=3.20.0`
- `PyCryptodome`
- `moomoo-api`

Research, testing, and optional feature dependencies include:

- `pytest`
- `ruff`
- `numpy`
- `matplotlib`
- `talib` or a compatible TA-Lib installation, if you enable TA features
- `optuna`, `quantstats`, and `vectorbt`, if you run optional research steps

This repository currently does not ship a pinned `requirements.txt`. Use an isolated virtual
environment and pin versions in your own deployment if you need reproducibility.

## Quick Start

### Run tests that do not need OpenD

```powershell
python -m pytest -q
python -m ruff check .
```

Run market-specific tests:

```powershell
python -m pytest us_strategy/tests -q
python -m pytest hk_strategy/tests -q
python -m pytest research/tests tools/tests -q
```

### Probe data availability

These commands require OpenD and the relevant market-data permissions:

```powershell
python -m us_strategy.probe US.AAPL US.TSLA
python -m hk_strategy.probe HK.00700 HK.09988
```

Run probes before enabling a symbol in a watchlist. Some moomoo endpoints are not uniformly
available across markets, accounts, data packages, or trading sessions.

### Run simulated strategies

Recommended launchers:

```powershell
powershell -ExecutionPolicy Bypass -File us_strategy\run_simulate.ps1
powershell -ExecutionPolicy Bypass -File hk_strategy\run_simulate.ps1
```

Direct module entrypoints also default to simulation:

```powershell
python -m us_strategy.main
python -m hk_strategy.main
```

Override watchlists temporarily:

```powershell
$env:WATCHLIST = "US.AAPL,US.TSLA"
python -m us_strategy.main

$env:WATCHLIST = "HK.00700,HK.09988"
python -m hk_strategy.main
```

Use `US.` prefixes for US symbols and `HK.` prefixes for HK symbols.

## Watchlists and IPO Flow

Runtime universe:

```text
today IPOs + watchlist + current broker/local positions
```

Watchlist sources:

| Source | Behavior |
| --- | --- |
| `WATCHLIST` env var | Highest priority, comma-separated codes |
| `WATCHLIST_FILE` env var | Custom watchlist file path |
| `us_strategy/watchlist.txt` | Default US watchlist |
| `hk_strategy/watchlist.txt` | Default HK watchlist |

Watchlist file format:

```text
# comments are allowed
US.AAPL
US.TSLA,US.NVDA
```

IPO watchlist files use:

```text
YYYY-MM-DD<TAB>CODE<TAB>NAME<TAB>LIST_TIME
```

Today-IPO entries are stored separately from mature-stock watchlists:

- `us_strategy/ipo_watchlist.txt`
- `hk_strategy/ipo_watchlist.txt`

Today IPOs have their own sizing and risk profile:

| Setting | Default |
| --- | ---: |
| `IPO_POSITION_RATIO` | `0.05` |
| `IPO_ENTRY_TRANCHES` | `2` |
| `IPO_TAKE_PROFIT_PCT` | `0.12` |
| `IPO_STOP_LOSS_PCT` | `0.06` |
| `IPO_TRAILING_STOP_PCT` | `0.08` |

If real prices or turnover are not ready, the IPO candidate is observed and blocked from trading
until it becomes analyzable.

## Factor Model

All `*_score` values are 0-100 risk scores:

```text
0   = low risk / more bullish
100 = high risk / more bearish
```

For a useful factor, forward IC should usually be significantly negative.

Default active decision weights:

| Factor | Default weight |
| --- | ---: |
| `capital` | `0.55` |
| `turnover` | `0.25` |
| `momentum` | `0.20` |

Extended factors are usually disabled or weight-zero until forward IC validates them:

| Group | Example score keys | Main source | Default state |
| --- | --- | --- | --- |
| Core | `turnover`, `capital`, `momentum` | snapshot, capital distribution, candles | Active |
| Technical | `orb`, `rs`, `vwap` | historical K-line, intraday candles | Off / zero weight |
| Microstructure | `order_flow`, `dark_pool_proxy`, `obi`, `l2_imbalance`, `intraday_flow` | ticker, order book, capital flow | Off / zero weight |
| HK-specific | `broker`, `hk_status` | broker queue, `dark_status`, `sec_status` | Off / zero weight |
| Short side | `short` | short interest, short volume | Off / zero weight |
| Options | `option_iv` | option chain and snapshots | Warning-only by default |

`dark_pool_proxy` is only a proxy based on large visible moomoo real-time ticks. It is not FINRA TRF
dark-pool data.

## Forward IC Calibration

Microstructure factors generally do not have a full historical replay source. They are collected
forward and calibrated over time:

```text
forward_monitor.py
    -> writes signal_log with scores@T and price@T
ic_report.py
    -> computes daily cross-sectional forward IC for 15/30 minute horizons
    -> writes ic_history
IC gate
    -> >= 20 trading days
    -> |mean IC| > 0.03
    -> |IR| > 0.5
    -> expected sign is negative
```

Commands:

```powershell
python -m us_strategy.forward_monitor
python -m hk_strategy.forward_monitor

python -m us_strategy.ic_report
python -m hk_strategy.ic_report
```

Forward monitors only log scores. They do not place orders.

## Research and Backtesting

Backtest report CLI:

```powershell
python -m research.run_backtest_report --market us --codes US.AAPL,US.MSFT --start 2024-01-01 --end 2024-03-31
python -m research.run_backtest_report --market hk --codes HK.00700,HK.09988 --start 2024-01-01 --end 2024-03-31
```

Use local SQLite history as a research data source:

```powershell
python -m research.run_backtest_report --market us --codes US.AAPL,US.MSFT --start 2024-01-01 --end 2024-03-31 --source sqlite --sqlite-db us_strategy/history_data.db
```

Signal research CLI:

```powershell
python -m research.signal_lab --market us --codes US.AAPL,US.MSFT --start 2025-01-01 --end 2025-12-31 --steps ic,walkforward
python -m research.signal_lab --market hk --codes HK.00700,HK.09988 --start 2025-01-01 --end 2025-12-31 --steps ic,walkforward
```

Optional research steps:

- `ic`
- `walkforward`
- `optuna`
- `quantstats`
- `vectorbt`

Default research outputs:

| CLI | Default output directory |
| --- | --- |
| `research.signal_lab` | `report/outputs/signal_research` |
| `research.run_backtest_report` | `report/outputs/backtest_report` |

`report/outputs/` is ignored by Git.

## Data Persistence

Strategy SQLite databases are local runtime state and are ignored by Git:

| Default DB | Market |
| --- | --- |
| `us_strategy/positions.db` | US |
| `hk_strategy/positions.db` | HK |

Key strategy tables:

| Table | Purpose |
| --- | --- |
| `positions` | Position recovery with quantity, weighted cost, and origin |
| `signal_log` | Forward signal log: timestamp, code, price, score JSON, market session |
| `ic_history` | Daily factor IC: date, factor, horizon, IC, sample size |

Historical market and microstructure data is usually stored in `us_strategy/history_data.db`:

| Table family | Purpose |
| --- | --- |
| `history_kline`, `market_snapshot` | Historical candles and after-hours snapshots |
| `hk_market_status_snapshots` | HK `dark_status` and `sec_status` snapshots |
| `realtime_ticks`, `realtime_quote_snapshots` | Intraday tick and quote snapshots |
| `order_book_snapshots`, `order_book_levels`, `order_book_metrics` | L2 order book data |
| `broker_queue_snapshots`, `broker_queue_levels`, `broker_queue_metrics` | HK broker queue data |
| `dark_pool_proxy_events`, `dark_pool_proxy_metrics` | Large visible-trade proxy events |
| `l2_imbalance_signals`, `microstructure_alerts` | Imbalance signals and alerts |
| `microstructure_daily_features` | Daily aggregated microstructure features |
| `backfill_runs`, `tick_runs` | Collection audit records |

## Data Collection and Health Checks

Historical backfill:

```powershell
python -m tools.daily_moomoo_watchlist_backfill --markets US,HK --db us_strategy/history_data.db
```

Realtime tick collection:

```powershell
python -m tools.collect_moomoo_ticks --codes US.AAPL,HK.00700 --markets US,HK --duration-seconds 60 --db us_strategy/history_data.db
```

Microstructure features:

```powershell
python -m tools.microstructure_features --db us_strategy/history_data.db --codes US.AAPL,HK.00700
```

Read-only health checks:

```powershell
python -m tools.check_moomoo_data_health --db us_strategy/history_data.db --markets US,HK
python -m tools.check_moomoo_tasks --root D:\moomoo-quant --json --strict
```

`tools.check_moomoo_tasks` validates Windows Task Scheduler metadata without modifying tasks.

## Windows Scheduled Tasks

Scheduled tasks are not part of the Git repository, but the PowerShell launchers are.

| Task | Trigger | Purpose |
| --- | --- | --- |
| `MoomooForwardCollect` | Mon-Fri 16:00 Beijing | US forward signal collection across PRE/AFTER sessions |
| `MoomooICReport` | Tue-Sat 06:30 Beijing | US IC report after US close |
| `MoomooHKForwardCollect` | Mon-Fri 09:15 Beijing | HK forward signal collection |
| `MoomooHKICReport` | Mon-Fri 16:30 Beijing | HK IC report after HK close |
| `MoomooUSDailyWatchlistBackfill` | Daily 06:30 Beijing | US/HK watchlist historical backfill |
| `MoomooUSTickCollect` | Mon-Fri 21:00 Beijing | US intraday tick, quote, order-book, imbalance collection |
| `MoomooUSSimTrade` | Mon-Fri 21:15 Beijing | US simulated strategy runtime |
| `MoomooHKTickCollect` | Mon-Fri 09:15 Beijing | HK intraday tick, quote, order-book, broker-queue collection |
| `MoomooHKSimTrade` | Mon-Fri 09:15 Beijing | HK simulated strategy runtime |

PowerShell scripts are launchers only. Complex JSON, report rendering, Feishu/Lark payloads, data
parsing, and validation should live in Python CLIs.

## Feishu / Lark Alerts

Alerting is optional and environment-driven:

| Variable | Purpose |
| --- | --- |
| `FEISHU_CHAT_ID` | Target Feishu/Lark chat ID |
| `LARK_CLI` | Path or command name for `lark-cli` |
| `ALERT_EMAIL` | Email alert target |
| `TELEGRAM_TOKEN` | Telegram bot token |
| `TELEGRAM_CHAT_ID` | Telegram chat ID |

For long reports, use the established pattern:

```text
full Markdown report -> Feishu cloud document
summary -> interactive card in group chat
online readback -> verify message_id, card title, key fields, and document URL
```

Do not send long Markdown reports directly as chat text; that path is prone to truncation in
Windows PowerShell and CLI quoting contexts.

## Environment Variables

Most settings are optional and have defaults.

| Category | Variables |
| --- | --- |
| OpenD | `OPEND_HOST`, `OPEND_PORT` |
| Trading mode | `TRADE_ENV`, `ALLOW_REAL_TRADING`, `TRADE_PASSWORD` |
| Universe | `WATCHLIST`, `WATCHLIST_FILE`, `IPO_DAYS_WINDOW`, `IPO_WATCHLIST_FILE` |
| HK liquidity | `MIN_DAILY_TURNOVER` |
| Position sizing | `POSITION_RATIO`, `MAX_POSITIONS`, `ENTRY_TRANCHES`, `ORDER_LOTS_PER_TRADE`, `USE_ATR_SIZING` |
| IPO sizing | `IPO_POSITION_RATIO`, `IPO_ENTRY_TRANCHES` |
| Risk | `STOP_LOSS_PCT`, `TRAILING_STOP_PCT`, `MIN_HOLD_DAYS`, `DAILY_LOSS_LIMIT_PCT`, `CIRCUIT_BREAKER_BASELINE` |
| IPO risk | `IPO_TAKE_PROFIT_PCT`, `IPO_STOP_LOSS_PCT`, `IPO_TRAILING_STOP_PCT` |
| Execution | `USE_LIMIT_ORDERS`, `LIMIT_PRICE_TOLERANCE_PCT`, `TRADE_FAILURE_ALERT_COOLDOWN_S` |
| Factor switches | `USE_RS`, `USE_ORB`, `USE_VWAP_SIGNAL`, `USE_ORDER_FLOW`, `USE_DARK_POOL_PROXY`, `USE_ORDER_BOOK_IMBALANCE`, `USE_L2_IMBALANCE_TRACKER`, `USE_INTRADAY_FLOW`, `USE_SHORT_METRICS`, `USE_OPTION_IV`, `USE_BROKER_SIGNAL`, `USE_BROKER_GATE`, `USE_HK_STATUS_SIGNAL` |
| Alerts | `ALERT_EMAIL`, `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `FEISHU_CHAT_ID`, `LARK_CLI` |
| Calibration | `MONITOR_INTERVAL_S`, `MONITOR_MAX_ROUNDS`, `IC_HORIZONS`, `IC_MIN_DAYS`, `DB_PATH` |

See the docstrings at the top of `us_strategy/main.py` and `hk_strategy/main.py` for the most
current runtime list.

## Moomoo API Rate Limits

Repository-level rate-limit facts live in `moomoo_rate_limits.py`. Strategy configs default to a
conservative global token bucket of `28` requests per `30` seconds for `DataAccess`, leaving margin
below known 30-per-30-second endpoints.

| API family | Conservative rule | Notes |
| --- | ---: | --- |
| `get_market_snapshot` | 60 / 30s | Single call supports many symbols, subject to data package limits |
| `request_history_kline` | 60 / 30s | First page is rate-limited; pagination behavior differs |
| `get_capital_flow`, `get_capital_distribution` | 30 / 30s | Covered by the default 28 / 30s global bucket |
| `get_option_expiration_date` | 60 / 30s | Pre-query for option chain |
| `get_option_chain` | 10 / 30s | Default tooling sleeps around 3 seconds |
| Trade queries with `refresh_cache=True` | 10 / 30s / account | Strategy defaults usually read OpenD cache |
| `place_order` | 15 / 30s / account | Live use still requires manual review and unlock gates |
| Subscribed quote/order-book/ticker reads | OpenD cache | Not counted as server requests, but subject to subscription quota |

Unknown or low-frequency endpoints should be treated conservatively unless the official current
documentation says otherwise.

## Testing and Validation

Recommended local validation:

```powershell
python -m pytest -q
python -m ruff check .
python -m tools.check_moomoo_tasks --root D:\moomoo-quant --json --strict
git diff --check
```

For low-side-effect syntax validation on Windows, prefer an AST parse pass instead of commands that
write `__pycache__` files:

```powershell
python -c "import ast, pathlib; [ast.parse(p.read_text(encoding='utf-8-sig'), filename=str(p)) for p in pathlib.Path('.').rglob('*.py') if '.venv' not in p.parts]"
```

Before changing strategy logic, validate at least:

- Unit tests for the affected market package.
- Backtest report on the affected universe.
- Forward IC or historical IC evidence for any factor weight change.
- No new live-trading bypass.
- No PowerShell JSON or report-generation logic added outside Python.

## Release Highlights

### v1.9.0

- Deep audit of the main strategy, signals, and backtest logic; most agent-flagged "critical" issues were verified against source as false positives (textbook Sortino downside deviation, Wilder ATR, intentional PCR fallback, filter-style macro/crypto gates, no look-ahead in the daily-close fill model) and deliberately left unchanged.
- Fixed `option_iv` being computed but silently dropped: `active_weights()` now registers it when `use_option_iv` is on, consistent with sibling factors (US and HK).
- Fixed walk-forward segments sharing one boundary trading day; adjacent splits no longer overlap (US and HK).
- Order-book imbalance now aggregates levels with `1/level` distance decay instead of an equal mean, reducing level-1 quote noise.
- Added an opt-in intraday staleness gate for the order-flow factor (`ORDER_FLOW_MAX_STALENESS_SECONDS`, default `0` = disabled).
- Added an opt-in IPO-origin lifecycle downgrade (`IPO_ORIGIN_MAX_HOLD_DAYS`, default `0` = disabled): aged IPO positions revert to regular exit rules.
- Hardened partial-fill accounting in the limit-order executor: prefer the re-queried real filled quantity over assuming the full request size after a failed cancel.
- Sharpe/Sortino now support a configurable annual risk-free rate (`ANNUAL_RISK_FREE_RATE`), enabled by default at ≈4% (US) and ≈3.5% (HK); set `0` to restore the prior zero-rate reporting.
- Added focused unit tests for every change; full suite green (US + HK).

### v1.8.0

- Added independent today-IPO simulation flow for US and HK.
- Today IPOs no longer mix into the regular watchlist.
- Added IPO-specific position sizing, take profit, stop loss, and trailing stop profiles.
- IPO positions persist with `origin=ipo`, so restarts keep the correct risk profile.
- Added Feishu/Lark alerts for IPO discovery, first analyzable state, trades, risk exits, and key blocks.
- `tools.check_moomoo_tasks` accepts running Windows Task Scheduler status values that are not errors.
- Local validation for the release recorded full pytest, ruff, and whitespace checks, with one known historical task-state warning for `MoomooHKTickCollect`.

### v1.7.0

- Improved US/HK simulation execution loop and account snapshot alerts.
- Added separate simulated-account sizing for US and HK.
- Added fill-timeout cancellation checks.
- Added `tools/liquidate_us_sim_positions.py` with simulation-only safeguards.
- Extended task validation for simulation launcher parameters.

### v1.6.0

- Added US/HK scheduled automation loop.
- Added HK simulated trading scheduler.
- Added Feishu interactive-card alerting.
- Added SQLite research source support for `research.signal_lab` and `research.run_backtest_report`.
- Added HK benchmark support through `HK.800000`.
- Expanded task and data-health checks.

## Development Rules

Repository-specific development guidance lives in `AGENTS.md`.

Important project rules:

- Use UTF-8 for text, Markdown, Python, configs, and generated report content.
- PowerShell files should remain launchers; business logic belongs in Python CLIs.
- Prefer `subprocess.run([...])` argument arrays for external CLI calls.
- Do not hard-code API keys, passwords, chat IDs, or account credentials.
- Do not add dependencies without an explicit reason and installation notes.
- Do not enable live trading by default.
- Do not treat `receipt=sent` as final alert validation; use online readback when sending Feishu/Lark messages.

## Known Limitations

- OpenD, market-data permission, account type, and region-specific endpoint availability determine what can actually run.
- Some moomoo endpoints differ between US and HK markets.
- Microstructure factors need forward collection before they can be trusted.
- Local SQLite data and generated reports are not included in the repository.
- No pinned lockfile is currently provided.
- The repository currently has no open-source license file.

## License

No open-source license has been declared yet. Public visibility on GitHub does not automatically
grant permission to copy, modify, redistribute, or use this project commercially.

Contact the repository owner before reusing the code outside personal research.
