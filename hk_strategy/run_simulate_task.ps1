# run_simulate_task.ps1 - scheduled launcher for HK simulated trading.
#
# Runs `python -m hk_strategy.main` against the moomoo simulated account only.
# Registered by: Register-ScheduledTask "MoomooHKSimTrade"
#   Trigger : weekly Mon-Fri 09:15 (Beijing/Hong Kong)
#   Limit   : 8h run window -> stops after the regular HK session

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$py = 'C:\Users\MrLee\AppData\Local\Programs\Python\Python314\python.exe'
$logDir = Join-Path $root 'logs'
$log = Join-Path $logDir ("hk_sim_trade_{0}.log" -f (Get-Date -Format 'yyyyMMdd'))

Set-Location $root
New-Item -ItemType Directory -Path $logDir -Force | Out-Null

function Write-Log($message) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Encoding UTF8 -Value "[$ts] $message"
}

$env:PYTHONUTF8 = '1'
$env:TRADE_ENV = 'SIMULATE'
$env:MAX_POSITIONS = '0'
$env:FEISHU_CHAT_ID = 'oc_bc9a36b4392dbe632fb4e50a3ef7ef17'
Remove-Item Env:\ALLOW_REAL_TRADING -ErrorAction SilentlyContinue
Remove-Item Env:\TRADE_PASSWORD -ErrorAction SilentlyContinue

$conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 11111 -WarningAction SilentlyContinue
if (-not $conn.TcpTestSucceeded) {
    Write-Log 'OpenD (127.0.0.1:11111) not reachable; aborting this run.'
    exit 0
}

Write-Log 'launching hk_strategy.main in moomoo SIMULATE mode'
& cmd /c "`"$py`" -u -m hk_strategy.main >> `"$log`" 2>&1"
$exitCode = $LASTEXITCODE
Write-Log "hk_strategy.main exited (code=$exitCode)"
exit $exitCode
