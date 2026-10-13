param(
    [string]$TaskPath = "\Polymarket\",
    [switch]$IncludeBackup
)

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
