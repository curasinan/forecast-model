# Register the daily climate-label collector with Windows Task Scheduler.
#
# Run from an ELEVATED PowerShell with the repo root as the current directory:
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\register_climate_tasks.ps1
#
# Registration only -- nothing is run here. The task runs
# scripts\collect_climate_labels.py once a day, which fetches the NOAA
# settling series (GTM/GT temperature anomalies, Mauna Loa CO2) and appends
# any new first-print periods to the label ledger under
# ~/quant_data/climate_data (NOT AppData -- see MIGRATION_CHECKPOINT section 3).
# NOAA releases temperature in the first half of
# the month at 10:00 CT and CO2 on the 5th at 8:00 CT, so a daily run captures
# first prints within ~24h. The collector exits 0 when nothing is new
# (cron-friendly) and exits 1 only if a fetch fails.
#
# Uses Interactive logon (not S4U): S4U produced phantom runs on this machine
# (see docs/ibkr_runbook.md); this task only needs the network, so at-logon is
# fine. Idempotent: replaces an existing registration with the same name.

param(
    [string]$PythonExe = "python.exe",
    [string]$RepoRoot  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskPath  = "\Climate\",
    [string]$RunAt     = "16:30"  # local; after the US-morning NOAA release window
)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

$trigger = New-ScheduledTaskTrigger -Daily -At $RunAt

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 15)

$action = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "scripts\collect_climate_labels.py" `
    -WorkingDirectory $RepoRoot

Unregister-ScheduledTask -TaskPath $TaskPath -TaskName "Climate Labels" -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask `
    -TaskName "Climate Labels" `
    -TaskPath $TaskPath `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Principal $principal `
    -Description "Daily NOAA climate-label collector -> climate_data label ledger (first-print vintage)." | Out-Null

Write-Host "Registered: $TaskPath`Climate Labels (daily at $RunAt)"
Write-Host "Inspect:  Get-ScheduledTask -TaskPath '\Climate\' | Get-ScheduledTaskInfo"
Write-Host "Remove:   .\scripts\scheduling\unregister_climate_tasks.ps1"
