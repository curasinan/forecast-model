param(
    [string]$PythonExe = "python.exe",
    [string]$RepoRoot  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskPath  = "\IBKR\"
)

$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive `
    -RunLevel Limited

function Register-IbkrTask {
    param(
        [string]$TaskName,
        [Microsoft.Management.Infrastructure.CimInstance]$Action,
        [Microsoft.Management.Infrastructure.CimInstance[]]$Triggers,
        [Microsoft.Management.Infrastructure.CimInstance]$Settings,
        [string]$Description
    )

    # Idempotent: replace any existing registration with the same name.
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -Action $Action `
        -Trigger $Triggers `
        -Settings $Settings `
        -Principal $principal `
        -Description $Description | Out-Null

    Write-Host "Registered: $TaskPath$TaskName"
}

$now = Get-Date

$minutesToNext = 5 - ($now.Minute % 5)
$recorderSeed = $now.AddSeconds(-$now.Second).AddMilliseconds(-$now.Millisecond).AddMinutes($minutesToNext)
Write-Host "IBKR Recorder keep-alive first tick at: $recorderSeed"

$recorderTriggers = @(
    New-ScheduledTaskTrigger -AtLogOn
    New-ScheduledTaskTrigger `
        -Once `
        -At $recorderSeed `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650))
)

$recorderSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650)

$launcher = Join-Path $RepoRoot 'scripts\scheduling\run_ibkr_recorder.ps1'
# conhost --headless runs the task without a terminal window (it drops the exit code).
$recorderAction = New-ScheduledTaskAction `
    -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" `
    -WorkingDirectory $RepoRoot

Register-IbkrTask `
    -TaskName "IBKR Recorder" `
    -Action $recorderAction `
    -Triggers $recorderTriggers `
    -Settings $recorderSettings `
    -Description "ForecastEx quote sweeps (clientId 2, read-only) -> ~/quant_data/ibkr_ticks. Keep-alive: 5-min tick + IgnoreNew."

$healthSeed = $now.AddMinutes(-$now.Minute).AddSeconds(-$now.Second).AddMilliseconds(-$now.Millisecond).AddHours(1)
Write-Host "IBKR Recorder Health first fire at: $healthSeed"

$healthTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $healthSeed `
    -RepetitionInterval (New-TimeSpan -Hours 1) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))

$healthSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 5)

$healthLauncher = Join-Path $RepoRoot 'scripts\scheduling\run_ibkr_health.ps1'
$healthAction = New-ScheduledTaskAction `
    -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$healthLauncher`"" `
    -WorkingDirectory $RepoRoot

Register-IbkrTask `
    -TaskName "IBKR Recorder Health" `
    -Action $healthAction `
    -Triggers $healthTrigger `
    -Settings $healthSettings `
    -Description "Hourly recorder freshness + data-flow (DEGRADED) + paper-sync freshness (SYNC_STALE) check; alerts via Telegram, durable sink logs\ibkr_health.log."

$syncSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

$syncLauncher = Join-Path $RepoRoot 'scripts\scheduling\run_ibkr_sync.ps1'
$syncAction = New-ScheduledTaskAction `
    -Execute 'conhost.exe' `
    -Argument "--headless powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$syncLauncher`"" `
    -WorkingDirectory $RepoRoot

Register-IbkrTask `
    -TaskName "IBKR Paper Sync" `
    -Action $syncAction `
    -Triggers (New-ScheduledTaskTrigger -Daily -At "17:30") `
    -Settings $syncSettings `
    -Description "Daily paper-book reconciliation (clientId 4, read-only) -- reconciles resting GTC orders against the venue and writes the outcome to ibkr_paper.db."

Write-Host ""
Write-Host "Done. Registration only -- nothing was started; the keep-alive tick"
Write-Host "launches the recorder within 5 minutes (and at every boot), the health"
Write-Host "check fires hourly, and the paper sync fires daily at 17:30."
Write-Host "Inspect:  Get-ScheduledTask -TaskPath '\IBKR\' | Get-ScheduledTaskInfo"
Write-Host "Tail log: Get-Content logs\ibkr_recorder_task.log -Wait -Tail 20"
Write-Host "Tail log: Get-Content logs\ibkr_sync_task.log -Wait -Tail 20"
