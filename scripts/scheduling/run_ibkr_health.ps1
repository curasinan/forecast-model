# Launcher for the 'IBKR Recorder Health' scheduled task.
#
# Task Scheduler starts this with no profile and no .env-loading shell. The
# probe itself is Gateway-free by design (no ib_async import, filesystem
# only) and reads its alert config straight from os.environ, so unlike the
# recorder/sync launchers there are no IBKR_PORT / IBKR_TRADING_MODE
# defaults to set here -- HKCU user env vars (ALERT_METHOD,
# TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID) already reach a scheduled task
# without this script doing anything.
#
# stdout/stderr append to logs\ibkr_health_task.log (rotated to .1 at
# 50 MB). The probe's own durable non-OK sink is logs\ibkr_health.log (see
# scripts/check_ibkr_recorder.py). The python exit code is propagated as
# this script's exit code.

$ErrorActionPreference = 'Continue'

# Repo root is two levels above this script (scripts\scheduling\..\..).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

$logDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'ibkr_health_task.log'

# Size guard: rotate to a single .1 generation once the log passes 50 MB.
# Rotation happens BEFORE the append below, so one run never splits a
# session across the boundary mid-write.
if (Test-Path $logFile) {
    if ((Get-Item $logFile).Length -gt 50MB) {
        try { Move-Item -Force $logFile "$logFile.1" } catch { }
    }
}

# Launch marker: the hourly trigger fires every hour, so a run of these
# markers with nothing between them means the process is dying at startup
# -- check this file and logs\ibkr_health.log.
Add-Content -Path $logFile -Value ("--- task launch {0:yyyy-MM-dd HH:mm:ss}Z ---" -f (Get-Date).ToUniversalTime())

# cmd.exe does the redirection on purpose: Windows PowerShell 5.1 wraps a
# native command's redirected stderr lines in ErrorRecords
# (NativeCommandError), which garbles the log. cmd appends both streams
# verbatim and passes python's exit code through as its own.
& cmd.exe /d /c "python -u scripts\check_ibkr_recorder.py >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
