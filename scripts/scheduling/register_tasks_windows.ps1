# Register the two collector jobs with Windows Task Scheduler.
#
# Run from an ELEVATED PowerShell (right-click -> Run as administrator)
# with the repo root as the current directory:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\register_tasks_windows.ps1
#
# The -PythonExe parameter can be overridden if `python` on PATH is not
# the interpreter you want the tasks to use.

param(
    [string]$PythonExe = "python.exe",
    [string]$RepoRoot  = (Get-Location).Path,
    [string]$TaskPath  = "\Polymarket\"
)

function Register-CollectorTask {
    param(
        [string]$TaskName,
        [string]$Module,
        [Microsoft.Management.Infrastructure.CimInstance[]]$Triggers
    )

    $action = New-ScheduledTaskAction `
        -Execute $PythonExe `
        -Argument "-m $Module" `
        -WorkingDirectory $RepoRoot

    # IgnoreNew == if a previous instance is still running, do not start
    # another. This is the scheduler-level overlap guard; the Python
    # scripts also guard via a PID lock for defence in depth.
    $settings = New-ScheduledTaskSettingsSet `
        -MultipleInstances IgnoreNew `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -ExecutionTimeLimit (New-TimeSpan -Hours 1)

    # Replace any existing registration with the same name.
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue

    Register-ScheduledTask `
        -TaskName $TaskName `
        -TaskPath $TaskPath `
        -Action $action `
        -Trigger $Triggers `
        -Settings $settings `
        -Description "Polymarket collector: $Module" | Out-Null

    Write-Host "Registered: $TaskPath$TaskName"
}

# --- collect_markets: 06:00 and 18:00 daily ---------------------------------
$marketsTriggers = @(
    New-ScheduledTaskTrigger -Daily -At 6:00am
    New-ScheduledTaskTrigger -Daily -At 6:00pm
)
Register-CollectorTask -TaskName "collect_markets" -Module "collectors.collect_markets" -Triggers $marketsTriggers

# --- collect_snapshots: every 15 minutes, forever ---------------------------
# Seed the first trigger at the NEXT quarter-hour boundary from now, then
# repeat every 15 minutes indefinitely. Seeding at midnight-tomorrow (a
# natural-looking choice) would delay the first run by up to 24h.
$now = Get-Date
$minutesToNext = 15 - ($now.Minute % 15)
$seed = $now.AddSeconds(-$now.Second).AddMilliseconds(-$now.Millisecond).AddMinutes($minutesToNext)
Write-Host "collect_snapshots first fire at: $seed"

$snapshotTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At $seed `
    -RepetitionInterval (New-TimeSpan -Minutes 15) `
    -RepetitionDuration ([TimeSpan]::FromDays(3650))

Register-CollectorTask -TaskName "collect_snapshots" -Module "collectors.collect_snapshots" -Triggers $snapshotTrigger

Write-Host ""
Write-Host "Done. View in Task Scheduler under '$TaskPath'."
Write-Host "Tail logs:  Get-Content logs\snapshot_collector.log -Wait -Tail 20"
