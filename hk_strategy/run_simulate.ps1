# run_simulate.ps1 - launch the HK strategy in moomoo simulated trading.

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:TRADE_ENV = 'SIMULATE'
$env:MAX_POSITIONS = '13'
$env:POSITION_RATIO = '0'
$env:ENTRY_TRANCHES = '2'
$env:ORDER_LOTS_PER_TRADE = '0'
$env:IPO_WATCHLIST_FILE = 'hk_strategy\ipo_watchlist.txt'
$env:IPO_POSITION_RATIO = '0.05'
$env:IPO_ENTRY_TRANCHES = '2'
$env:IPO_TAKE_PROFIT_PCT = '0.12'
$env:IPO_STOP_LOSS_PCT = '0.06'
$env:IPO_TRAILING_STOP_PCT = '0.08'
$env:USE_LIMIT_ORDERS = 'false'
$env:ORDER_FILL_TIMEOUT_S = '30'
$env:ORDER_POLL_INTERVAL_S = '1'
$env:FEISHU_CHAT_ID = 'oc_bc9a36b4392dbe632fb4e50a3ef7ef17'
Remove-Item Env:\ALLOW_REAL_TRADING -ErrorAction SilentlyContinue
Remove-Item Env:\TRADE_PASSWORD -ErrorAction SilentlyContinue

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

python -m hk_strategy.main
