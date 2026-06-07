# register_moomoo_hk_sim_trade_task.ps1 - register HK simulated trading task.
#
# This task launches only moomoo simulated trading. It does not set any real
# trading unlock variables and does not store trade passwords.

param(
    [switch] $DryRun
)

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'
$taskName = 'MoomooHKSimTrade'
$scriptPath = Join-Path $root 'hk_strategy\run_simulate_task.ps1'
$description = 'Moomoo HK simulated trading strategy. SIMULATE only; no real trading unlock.'

if (-not (Test-Path -LiteralPath $scriptPath)) {
    throw "Script not found: $scriptPath"
}

if ($DryRun) {
    Write-Host "TaskName: $taskName"
    Write-Host "Script: $scriptPath"
    Write-Host 'Trigger: weekly Monday-Friday 09:15'
    Write-Host 'ExecutionTimeLimit: 8h'
    Write-Host "Description: $description"
    exit 0
}

$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`"" `
    -WorkingDirectory $root

$trigger = New-ScheduledTaskTrigger `
    -Weekly `
    -DaysOfWeek @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday') `
    -At '09:15'

$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Hours 8)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description $description `
    -Force

Write-Host "Registered $taskName."
