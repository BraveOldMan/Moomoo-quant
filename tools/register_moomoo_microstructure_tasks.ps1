# register_moomoo_microstructure_tasks.ps1 - register read-only data tasks.
#
# Run once in an elevated PowerShell window after moomoo OpenD is installed.
# The registered tasks only collect market data into SQLite; they do not trade.

$ErrorActionPreference = 'Stop'
$root = 'D:\Moomoo-quant'

function Register-MoomooTask {
    param(
        [Parameter(Mandatory = $true)]
        [string] $TaskName,

        [Parameter(Mandatory = $true)]
        [string] $ScriptPath,

        [Parameter(Mandatory = $true)]
        [string[]] $DaysOfWeek,

        [Parameter(Mandatory = $true)]
        [string] $At
    )

    $absoluteScript = Join-Path $root $ScriptPath
    if (-not (Test-Path -LiteralPath $absoluteScript)) {
        throw "Script not found: $absoluteScript"
    }

    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$absoluteScript`"" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $DaysOfWeek -At $At
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 12)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Moomoo read-only realtime microstructure collection' `
        -Force
}

function Register-MoomooDailyTask {
    param(
        [Parameter(Mandatory = $true)]
        [string] $TaskName,

        [Parameter(Mandatory = $true)]
        [string] $ScriptPath,

        [Parameter(Mandatory = $true)]
        [string] $At
    )

    $absoluteScript = Join-Path $root $ScriptPath
    if (-not (Test-Path -LiteralPath $absoluteScript)) {
        throw "Script not found: $absoluteScript"
    }

    $action = New-ScheduledTaskAction `
        -Execute 'powershell.exe' `
        -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$absoluteScript`"" `
        -WorkingDirectory $root
    $trigger = New-ScheduledTaskTrigger -Daily -At $At
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 4)

    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description 'Moomoo read-only after-close watchlist backfill' `
        -Force
}

Register-MoomooDailyTask `
    -TaskName 'MoomooUSDailyWatchlistBackfill' `
    -ScriptPath 'us_strategy\daily_watchlist_backfill.ps1' `
    -At '06:30'

Register-MoomooTask `
    -TaskName 'MoomooHKTickCollect' `
    -ScriptPath 'hk_strategy\tick_collect.ps1' `
    -DaysOfWeek @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday') `
    -At '09:15'

Register-MoomooTask `
    -TaskName 'MoomooUSTickCollect' `
    -ScriptPath 'us_strategy\tick_collect.ps1' `
    -DaysOfWeek @('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday') `
    -At '21:00'

Write-Host 'Registered daily backfill, HK tick collection, and US tick collection.'
