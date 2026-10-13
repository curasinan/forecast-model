param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskPath = "\Spark\"
)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

# Seed the repeating trigger at the next top of the hour.
$now = Get-Date
$seed = $now.AddMinutes(-$now.Minute).AddSeconds(-$now.Second).AddMilliseconds(-$now.Millisecond).AddHours(1)

$triggers = @(
    New-ScheduledTaskTrigger -AtLogOn
    New-ScheduledTaskTrigger `
        -Once `
        -At $seed `
        -RepetitionInterval (New-TimeSpan -Hours 6) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650))
)

$settings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 10) `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1)

$launcher = Join-Path $RepoRoot 'scripts\scheduling\run_spark_pull.ps1'
$action = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" `
    -WorkingDirectory $RepoRoot

# Idempotent: replace any existing registration with the same name.
Unregister-ScheduledTask -TaskPath $TaskPath -TaskName "Spark Pull" -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask `
    -TaskName "Spark Pull" `
    -TaskPath $TaskPath `
    -Action $action `
    -Trigger $triggers `
    -Settings $settings `
    -Principal $principal `
    -Description "Pulls the Spark's live quant-data root and its daily backups into this machine's mirror; pings the external check offbox-pull when the pull succeeded." | Out-Null

Write-Host "Registered: ${TaskPath}Spark Pull (at logon and every 6 hours; first tick at $seed)"
Write-Host "Inspect:  Get-ScheduledTask -TaskPath '\Spark\' | Get-ScheduledTaskInfo"
Write-Host "Tail log: Get-Content logs\spark_pull_task.log -Tail 20"
