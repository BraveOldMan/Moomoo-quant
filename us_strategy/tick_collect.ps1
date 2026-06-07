# tick_collect.ps1 - scheduled launcher for US watchlist tick collection.
#
# Runs `python -m tools.collect_moomoo_ticks` for US watchlist symbols and
# stores realtime TICKER rows, L2 ORDER_BOOK snapshots, L2 imbalance,
# large-print proxy events, alerts, and daily microstructure features.
#
# Registered by: Register-ScheduledTask "MoomooUSTickCollect"
#   Trigger : weekly Mon-Fri 21:00 (Beijing)
#   Window  : 33300s -> stops around 06:15

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$py = 'C:\Users\MrLee\AppData\Local\Programs\Python\Python314\python.exe'
$log = Join-Path $root 'us_strategy\tick_collect.log'
Set-Location $root

function Write-Log($message) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$ts] $message"
}

$conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 11111 -WarningAction SilentlyContinue
if (-not $conn.TcpTestSucceeded) {
    Write-Log 'OpenD (127.0.0.1:11111) not reachable; aborting this run.'
    exit 0
}

Write-Log 'launching US tick collector'
& cmd /c "`"$py`" -u -m tools.collect_moomoo_ticks --markets US --db us_strategy\history_data.db --us-watchlist us_strategy\watchlist.txt --duration-seconds 33300 --cache-num 1000 --batch-size 500 --flush-interval 5 --dark-pool-us-min-notional 100000 --dark-pool-hk-min-notional 800000 --l2-imbalance-level 10 --l2-imbalance-warn 0.35 --l2-imbalance-danger 0.60 >> `"$log`" 2>&1"
$exitCode = $LASTEXITCODE
Write-Log "US tick collector exited (code=$exitCode)"

if ($exitCode -ne 0) {
    exit $exitCode
}
