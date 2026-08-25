# Remove leftover Polymarket collector jobs from Windows Task Scheduler.
#
# Run from an ELEVATED PowerShell:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\unregister_polymarket_tasks.ps1
#
# Idempotent: already-absent tasks are reported and skipped.
#
# DELIBERATELY NOT REMOVED: \Polymarket\backup_db. It backs up
# data/predictions.db, which still holds the preserved research history
# (questions, predictions, resolutions) -- the migration spec keeps the
# Polymarket RESEARCH pipeline; only its live collection is retired. Pass
# -IncludeBackup to remove it too, if you have decided otherwise.
#
# The task folder is only deleted once it is empty, so it survives while
# backup_db does.

param(
    [string]$TaskPath = "\Polymarket\",
    [switch]$IncludeBackup
)

# The first three were the original collectors; the next three were still
# registered on 2026-08-25, all pointing at code that now exits 2 unless
# POLYMARKET_RUNTIME=1 (heartbeat had been failing 0x1 for that reason --
# \IBKR\IBKR Recorder Health replaced it).
$taskNames = @(
    "collect_markets",
    "collect_snapshots",
    "weather_collect_cycle",
    "collect_llm_scores",
    "collect_news_volume",
    "heartbeat"
)
if ($IncludeBackup) { $taskNames += "backup_db" }

foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Not registered (skipped): $TaskPath$name"
        continue
    }
    try { Stop-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction Stop } catch { }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false
    Write-Host "Removed: $TaskPath$name"
}

try {
    $svc = New-Object -ComObject Schedule.Service
    $svc.Connect()
    $root = $svc.GetFolder("\")
    $folderName = $TaskPath.Trim("\")
    $folder = $root.GetFolder($folderName)
    if ($folder.GetTasks(0).Count -eq 0) {
        $root.DeleteFolder($folderName, 0)
        Write-Host "Removed empty task folder: $TaskPath"
    } else {
        Write-Host "Task folder $TaskPath not empty; left in place."
    }
} catch { }

if (-not $IncludeBackup) {
    Write-Host "Kept: $($TaskPath)backup_db (backs up the preserved research DB)."
}
Write-Host "Done. Polymarket collectors stay retired; IBKR jobs are under \IBKR\."
