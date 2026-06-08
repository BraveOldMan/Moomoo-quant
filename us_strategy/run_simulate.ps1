# run_simulate.ps1 - scheduled launcher for US simulated trading.
#
# Runs `python -m us_strategy.main` against the moomoo simulated account only.
# Registered by: Register-ScheduledTask "MoomooUSSimTrade"
#   Trigger : weekly Mon-Fri 21:15 (Beijing) — covers US RTH in EDT/EST
#   Limit   : 10h run window -> stops after the regular US session

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$py = 'C:\Users\MrLee\AppData\Local\Programs\Python\Python314\python.exe'
$log = Join-Path $root 'us_strategy\run_simulate.log'

Set-Location $root

function Write-Log($message) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$ts] $message"
}

$env:PYTHONUTF8 = '1'
$env:TRADE_ENV = 'SIMULATE'
$env:MAX_POSITIONS = '8'
$env:POSITION_RATIO = '0.06'
$env:ENTRY_TRANCHES = '2'
$env:ORDER_LOTS_PER_TRADE = '0'
$env:IPO_WATCHLIST_FILE = 'us_strategy\ipo_watchlist.txt'
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

$conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 11111 -WarningAction SilentlyContinue
if (-not $conn.TcpTestSucceeded) {
    Write-Log 'OpenD (127.0.0.1:11111) not reachable; aborting this run.'
    exit 0
}

Write-Log 'launching us_strategy.main in moomoo SIMULATE mode'
& cmd /c "`"$py`" -u -m us_strategy.main >> `"$log`" 2>&1"
Write-Log "us_strategy.main exited (code=$LASTEXITCODE)"
