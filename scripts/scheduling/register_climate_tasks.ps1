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
