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

$repoWsl = '/mnt/' + $repoRoot.Substring(0, 1).ToLower() + ($repoRoot.Substring(2) -replace '\\', '/')

& cmd.exe /d /c "python -u scripts\spark\heartbeat_ping.py --url-env HEARTBEAT_URL_OFFBOX -- wsl.exe -d Ubuntu -- bash $repoWsl/scripts/spark/pull_snapshot.sh >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
