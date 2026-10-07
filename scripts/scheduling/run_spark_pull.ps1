# Launcher for the 'Spark Pull' scheduled task (\Spark\Spark Pull).
#
# Pulls the Spark's live root and its daily backups into this machine's mirror
# (%USERPROFILE%\quant_data, which carries the marker file .mirror-of-spark)
# and, when the pull succeeded, pings the external check 'offbox-pull'. A
# laptop that has not pulled for three days is reported by that check, not by
# this script (docs/plans/2026-10-05-spark-runtime-move-design.md sections 6
# and 8).
#
# The ping address is a capability. scripts\spark\heartbeat_ping.py reads it
# from the user-level environment variable HEARTBEAT_URL_OFFBOX and never
# prints it; it is not written in this file, in the task definition or in the
# log.
#
# stdout/stderr append to logs\spark_pull_task.log (rotated to .1 at 50 MB).
# The exit code is the pull's: 0 done, 3 the mirror carries no marker (the
# pull refused; nothing was written), anything else is rsync's or ssh's.

$ErrorActionPreference = 'Continue'

# Repo root is two levels above this script (scripts\scheduling\..\..).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

$logDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'spark_pull_task.log'

# Size guard: rotate to a single .1 generation once the log passes 50 MB.
if (Test-Path $logFile) {
    if ((Get-Item $logFile).Length -gt 50MB) {
        try { Move-Item -Force $logFile "$logFile.1" } catch { }
    }
}

Add-Content -Path $logFile -Value ("--- task launch {0:yyyy-MM-dd HH:mm:ss}Z ---" -f (Get-Date).ToUniversalTime())

# rsync lives in WSL, so the pull runs there. The repo as WSL sees it:
# C:\a\b -> /mnt/c/a/b. The path must not contain a space (it does not).
$repoWsl = '/mnt/' + $repoRoot.Substring(0, 1).ToLower() + ($repoRoot.Substring(2) -replace '\\', '/')

# cmd.exe does the redirection on purpose: Windows PowerShell 5.1 wraps a
# native command's redirected stderr lines in ErrorRecords, which garbles the
# log (see run_ibkr_sync.ps1). cmd appends both streams verbatim and passes
# the exit code through.
& cmd.exe /d /c "python -u scripts\spark\heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- wsl.exe -d Ubuntu -- bash $repoWsl/scripts/spark/pull_snapshot.sh >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
