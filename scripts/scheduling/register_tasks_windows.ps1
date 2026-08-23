# RETIRED 2026-08-23 — Polymarket collectors are no longer scheduled.
#
# This script used to register collect_markets / collect_snapshots /
# weather_collect_cycle under \Polymarket\. Those jobs hit Gamma/CLOB.
# The trading venue is IBKR ForecastEx (climate lane).
#
# Running this script now UNREGISTERS the old Polymarket tasks (if present)
# and prints where to find the IBKR jobs. It does not create new PM tasks.
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\register_tasks_windows.ps1
#
# IBKR recorder:  .\scripts\scheduling\register_ibkr_tasks.ps1  (elevated)
# Explicit PM uninstall: .\scripts\scheduling\unregister_polymarket_tasks.ps1

param(
    [string]$TaskPath = "\Polymarket\"
)

Write-Host "Polymarket collectors are RETIRED. This script will not register them."
Write-Host "Unregistering any leftover tasks under $TaskPath ..."

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

Write-Host ""
Write-Host "IBKR jobs live under \IBKR\ (register_ibkr_tasks.ps1)."
Write-Host "Climate labels: python scripts/collect_climate_labels.py (schedule separately, elevated)."
Write-Host "Done."
