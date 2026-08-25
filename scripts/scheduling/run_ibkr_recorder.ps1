# Launcher for the 'IBKR Recorder' scheduled task.
#
# Task Scheduler starts this with no profile and no .env-loading shell, so
# the two REQUIRED IBKRSettings fields (IBKRSettings has no defaults for
# them by design -- src/venues/ibkr/settings.py) are set explicitly here:
# read-only data collector against the PAPER session, Gateway on
# 127.0.0.1:4002, clientId 2. Paper shares the live account's market-data
# subscriptions, so the quotes are the same feed; the mode must match
# whatever the Gateway is logged into or connect() fails loudly on the
# DU/DF account-prefix check. Values already present in the environment
# win, so a manual override still works:
#
#   $env:IBKR_TRADING_MODE = 'live'; .\scripts\scheduling\run_ibkr_recorder.ps1
#
# stdout/stderr append to logs\ibkr_recorder_task.log (rotated to .1 at
# 50 MB). The recorder's own structured log is logs\ibkr_recorder.log.
# The python exit code is propagated as this script's exit code.

$ErrorActionPreference = 'Continue'

if (-not $env:IBKR_PORT)         { $env:IBKR_PORT = '4002' }
if (-not $env:IBKR_TRADING_MODE) { $env:IBKR_TRADING_MODE = 'paper' }

# Repo root is two levels above this script (scripts\scheduling\..\..).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

$logDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'ibkr_recorder_task.log'

# Size guard: rotate to a single .1 generation once the log passes 50 MB.
# Rotation happens BEFORE the append below, so one run never splits a
# session across the boundary mid-write.
if (Test-Path $logFile) {
    if ((Get-Item $logFile).Length -gt 50MB) {
        try { Move-Item -Force $logFile "$logFile.1" } catch { }
    }
}

# Launch marker: the keep-alive trigger fires every 5 minutes, so a run of
# these markers with nothing between them means the process is dying at
# startup -- check this file and logs\ibkr_recorder.log.
Add-Content -Path $logFile -Value ("--- task launch {0:yyyy-MM-dd HH:mm:ss}Z ---" -f (Get-Date).ToUniversalTime())

# cmd.exe does the redirection on purpose: Windows PowerShell 5.1 wraps a
# native command's redirected stderr lines in ErrorRecords
# (NativeCommandError), which garbles the log. cmd appends both streams
# verbatim and passes python's exit code through as its own.
& cmd.exe /d /c "python -u scripts\record_ibkr_ticks.py --interval-minutes 20 >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
