# Register the IBKR recorder + health-check jobs with Windows Task Scheduler.
#
# Run from an ELEVATED PowerShell (right-click -> Run as administrator)
# with the repo root as the current directory:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\register_ibkr_tasks.ps1
#
# Registration ONLY -- nothing is started or run here. The every-5-minutes
# trigger launches the recorder within 5 minutes of registration (and again
# at every boot). Operational doc: docs/ibkr_runbook.md.
#
# Durability design (why these settings):
#   - 'IBKR Recorder' is a long-lived loop (sweeps every 20 min, forever).
#     AtStartup covers reboots; the every-5-min repeating trigger plus
#     -MultipleInstances IgnoreNew is the keep-alive: while the process is
#     alive each tick is a no-op, and if it dies the next tick restarts it
#     within 5 minutes. RestartCount/RestartInterval add a scheduler-level
#     retry on hard failure. ExecutionTimeLimit 0 = never kill it.
#   - Double-start is impossible even if Task Scheduler misbehaves: the
#     recorder owns clientId 2 exclusively, and the Gateway refuses a second
#     connection with the same id -- connect raises ClientIdInUseError, ids
#     are never auto-incremented (src/venues/ibkr/clients.py, single-writer
#     rule). The scheduler guard is convenience; the client-id map is the
#     real lock.

param(
    [string]$PythonExe = "python.exe",
    [string]$RepoRoot  = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskPath  = "\IBKR\"
)

# S4U = "run whether user is logged on or not" WITHOUT storing a password.
# The recorder must not depend on an interactive session. Known caveat:
# password-less / Microsoft-account sign-ins sometimes refuse S4U at run
# time (LastTaskResult 0x80070569 or 0x41303); the fallback procedure is in
# docs/ibkr_runbook.md ("Durability" section). Note IB Gateway itself is a
# GUI app and still needs the user session it was logged in from.
$principal = New-ScheduledTaskPrincipal `
    -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U `
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

# --- IBKR Recorder: at startup + keep-alive tick every 5 minutes ------------
# Seed the repeating trigger at the NEXT 5-minute boundary from now (seeding
# at midnight-tomorrow would delay the first run by up to 24h).
$minutesToNext = 5 - ($now.Minute % 5)
$recorderSeed = $now.AddSeconds(-$now.Second).AddMilliseconds(-$now.Millisecond).AddMinutes($minutesToNext)
Write-Host "IBKR Recorder keep-alive first tick at: $recorderSeed"

$recorderTriggers = @(
    New-ScheduledTaskTrigger -AtStartup
    New-ScheduledTaskTrigger `
        -Once `
        -At $recorderSeed `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration ([TimeSpan]::FromDays(3650))
)

# IgnoreNew == while an instance is running, every keep-alive tick is a
# no-op. [TimeSpan]::Zero serializes as PT0S = NO execution time limit
# (the recorder is meant to run forever).
$recorderSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

$launcher = Join-Path $RepoRoot 'scripts\scheduling\run_ibkr_recorder.ps1'
$recorderAction = New-ScheduledTaskAction `
    -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" `
    -WorkingDirectory $RepoRoot

Register-IbkrTask `
    -TaskName "IBKR Recorder" `
    -Action $recorderAction `
    -Triggers $recorderTriggers `
    -Settings $recorderSettings `
    -Description "ForecastEx quote sweeps (clientId 2, read-only) -> %LOCALAPPDATA%\ibkr_ticks. Keep-alive: 5-min tick + IgnoreNew."

# --- IBKR Recorder Health: hourly freshness check ---------------------------
# Seeded at the next top of the hour. The check script owns its own alerting
# (Telegram); Task Scheduler discards its stdout.
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

$healthAction = New-ScheduledTaskAction `
    -Execute $PythonExe `
    -Argument "scripts\check_ibkr_recorder.py" `
    -WorkingDirectory $RepoRoot

Register-IbkrTask `
    -TaskName "IBKR Recorder Health" `
    -Action $healthAction `
    -Triggers $healthTrigger `
    -Settings $healthSettings `
    -Description "Hourly recorder freshness check (heartbeat age, parquet dirs); alerts via Telegram."

Write-Host ""
Write-Host "Done. Registration only -- nothing was started; the keep-alive tick"
Write-Host "launches the recorder within 5 minutes (and at every boot)."
Write-Host "Inspect:  Get-ScheduledTask -TaskPath '\IBKR\' | Get-ScheduledTaskInfo"
Write-Host "Tail log: Get-Content logs\ibkr_recorder_task.log -Wait -Tail 20"
