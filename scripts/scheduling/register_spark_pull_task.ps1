# Register the Zenbook's scheduled pull of the Spark's live root (\Spark\Spark Pull).
#
# Run from an ELEVATED PowerShell with the repo root as the current directory:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\register_spark_pull_task.ps1
#
# Registration ONLY -- nothing is pulled here. The task runs at logon and every
# six hours while the laptop is on. A run missed while it slept starts when it
# wakes (StartWhenAvailable) and waits for a network
# (RunOnlyIfNetworkAvailable). A run that fails -- the tailnet is not up yet,
# the Spark is unreachable -- is retried three times, ten minutes apart, and
# after that simply waits for the next tick: the external check 'offbox-pull'
# is what reports a copy that is getting old.
#
# Before the first run:
#   * the account holder sets the ping address for this Windows user, typed at a
#     prompt so that it does not land in the shell's history file:
#       [Environment]::SetEnvironmentVariable('HEARTBEAT_URL_OFFBOX', (Read-Host 'ping address of offbox-pull'), 'User')
#   * the mirror carries its marker (the pull refuses otherwise): a cutover step.
#
# Remove:  Unregister-ScheduledTask -TaskPath '\Spark\' -TaskName 'Spark Pull' -Confirm:$false

param(
    [string]$RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path,
    [string]$TaskPath = "\Spark\"
)

# Interactive, as for the IBKR tasks: S4U never executed an action on this
# machine (register_ibkr_tasks.ps1 records the finding), and the pull needs the
# user's ssh key and WSL, which live in the logged-on session.
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
