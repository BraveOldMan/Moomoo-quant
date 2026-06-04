# forward_collect.ps1 — scheduled-task launcher for forward-log collection.
#
# Runs `python -m us_strategy.forward_monitor`, which scores the watchlist on a
# loop and persists every factor score into signal_log WITHOUT placing orders.
# The monitor self-gates US market hours (skips rounds when closed), so this
# launcher only needs a generous daily window; the scheduled task supplies it.
#
# Registered by: Register-ScheduledTask "MoomooForwardCollect"
#   Trigger : weekly Mon-Fri 21:00 (Beijing) — covers US RTH in both EDT/EST
#   Limit   : 9h run window -> stops ~06:00 Beijing, after either DST close
#
# Purpose: accumulate (factor scores @T, price @T) so analysis.forward_ic_from_log
# can calibrate the un-validated microstructure / short / option factors.

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$py   = 'C:\Users\MrLee\AppData\Local\Programs\Python\Python314\python.exe'
$log  = Join-Path $root 'us_strategy\forward_monitor.log'
Set-Location $root

function Write-Log($message) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$ts] $message"
}

# Pre-requisite: OpenD gateway must be reachable, else exit cleanly so the task
# does not spin a process that can never connect.
$conn = Test-NetConnection -ComputerName 127.0.0.1 -Port 11111 -WarningAction SilentlyContinue
if (-not $conn.TcpTestSucceeded) {
    Write-Log 'OpenD (127.0.0.1:11111) not reachable; aborting this run.'
    exit 0
}

$env:MONITOR_INTERVAL_S = '300'
Write-Log 'launching forward_monitor (interval=300s)'
# forward_monitor logs to stderr. Redirect via cmd.exe (OS-level >> 2>&1) rather
# than PowerShell's *>> : under PowerShell 5.1 a native program's stderr is
# wrapped as a NativeCommandError (noise in the log, and a terminating error
# under EAP='Stop'), and *>> writes UTF-16. cmd appends raw UTF-8 cleanly.
& cmd /c "`"$py`" -u -m us_strategy.forward_monitor >> `"$log`" 2>&1"
Write-Log "forward_monitor exited (code=$LASTEXITCODE)"
