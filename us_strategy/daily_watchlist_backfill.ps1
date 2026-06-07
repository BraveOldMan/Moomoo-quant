# daily_watchlist_backfill.ps1 - scheduled launcher for US/HK watchlist data.
#
# Runs after the US close and stores:
#   1) adjusted daily K-line history for US/HK watchlist symbols
#   2) after-close get_market_snapshot rows for each market target date
#
# Registered by: Register-ScheduledTask "MoomooUSDailyWatchlistBackfill"
#   Trigger : daily 06:30 Beijing
#   Safety  : the Python job skips markets that did not trade on target date

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$py = 'C:\Users\MrLee\AppData\Local\Programs\Python\Python314\python.exe'
$log = Join-Path $root 'us_strategy\daily_watchlist_backfill.log'
Set-Location $root

function Write-Log($message) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$ts] $message"
}

Write-Log 'launching daily_moomoo_watchlist_backfill'
& cmd /c "`"$py`" -u -m tools.daily_moomoo_watchlist_backfill --db us_strategy\history_data.db --us-watchlist us_strategy\watchlist.txt --us-proxy-watchlist us_strategy\proxy_watchlist.txt --hk-watchlist hk_strategy\watchlist.txt --markets US,HK --history-start 2024-01-01 --sleep 0.2 --after-close-delay-min 90 >> `"$log`" 2>&1"
$exitCode = $LASTEXITCODE
Write-Log "daily_moomoo_watchlist_backfill exited (code=$exitCode)"

if ($exitCode -ne 0) {
    exit $exitCode
}
