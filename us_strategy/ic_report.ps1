# ic_report.ps1 — scheduled-task launcher for the daily forward-IC health check.
#
# Runs `python -m us_strategy.ic_report`, which recomputes per-day forward IC for
# every factor from signal_log, upserts into the ic_history table, and prints the
# cumulative IC/IR evolution. Read-only on market data — needs NO OpenD, just the
# local positions.db, so it can run any time after the session has been logged.
#
# Registered by: Register-ScheduledTask "MoomooICReport"
#   Trigger : weekly Tue-Sat 06:30 (Beijing) — after the US close + forward_collect
#             window (which stops ~06:00 Beijing) have finished for that session.
#
# Output is appended to us_strategy\ic_report.log (gitignored via *.log).

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$py   = 'C:\Users\MrLee\AppData\Local\Programs\Python\Python314\python.exe'
$log  = Join-Path $root 'us_strategy\ic_report.log'
Set-Location $root

function Write-Log($message) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $log -Value "[$ts] $message"
}

Write-Log '=== ic_report run start ==='
# Force Python to emit UTF-8 so the Chinese report is not mangled by the OEM
# code page when cmd appends stdout to the log (read it back with -Encoding UTF8).
$env:PYTHONUTF8 = '1'
# Redirect via cmd.exe (OS-level >> 2>&1): python logging writes to stderr, which
# PowerShell 5.1 would otherwise wrap as NativeCommandError / write as UTF-16.
$ErrorActionPreference = 'Continue'
& cmd /c "`"$py`" -u -m us_strategy.ic_report >> `"$log`" 2>&1"
Write-Log "ic_report exited (code=$LASTEXITCODE)"
