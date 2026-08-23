# Remove leftover Polymarket collector jobs from Windows Task Scheduler.
#
# Run from an ELEVATED PowerShell:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\unregister_polymarket_tasks.ps1
#
# Idempotent: already-absent tasks are reported and skipped.

param(
    [string]$TaskPath = "\Polymarket\"
)

$taskNames = @("collect_markets", "collect_snapshots", "weather_collect_cycle")

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

Write-Host "Done. Polymarket collectors stay retired; IBKR jobs are under \IBKR\."
