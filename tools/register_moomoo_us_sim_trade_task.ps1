# register_moomoo_us_sim_trade_task.ps1 - register US simulated trading task.
#
# This task launches only moomoo simulated trading. It does not set any real
# trading unlock variables and does not store trade passwords.

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$scriptPath = Join-Path $root 'us_strategy\run_simulate.ps1'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Script not found: $scriptPath"
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday') `
    -At '21:15'

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 10)

Register-ScheduledTask `
    -TaskName 'MoomooUSSimTrade' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'Moomoo US simulated trading strategy. SIMULATE only; no real trading unlock.' `
    -Force

Write-Host 'Registered MoomooUSSimTrade.'
