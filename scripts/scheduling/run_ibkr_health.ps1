$ErrorActionPreference = 'Continue'

# Repo root is two levels above this script (scripts\scheduling\..\..).
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $repoRoot

$logDir = Join-Path $repoRoot 'logs'
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
$logFile = Join-Path $logDir 'ibkr_health_task.log'

if (Test-Path $logFile) {
    if ((Get-Item $logFile).Length -gt 50MB) {
        try { Move-Item -Force $logFile "$logFile.1" } catch { }
    }
}

Add-Content -Path $logFile -Value ("--- task launch {0:yyyy-MM-dd HH:mm:ss}Z ---" -f (Get-Date).ToUniversalTime())

& cmd.exe /d /c "python -u scripts\check_ibkr_recorder.py >> `"$logFile`" 2>&1"
exit $LASTEXITCODE
