# Remove the IBKR recorder, health-check and paper-sync jobs from Windows Task Scheduler.
#
# Run from an ELEVATED PowerShell:
#
#   cd C:\Users\asus\OneDrive\Desktop\new_work
#   .\scripts\scheduling\unregister_ibkr_tasks.ps1
#
# Stops any running instance first, then unregisters, then removes the
# \IBKR\ task folder if it ended up empty. Idempotent: already-absent tasks
# are reported and skipped.

param(
    [string]$TaskPath = "\IBKR\"
)

$taskNames = @("IBKR Recorder", "IBKR Recorder Health", "IBKR Paper Sync")

foreach ($name in $taskNames) {
    $task = Get-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Not registered (skipped): $TaskPath$name"
        continue
    }
    # Stop a running instance so the unregister leaves no orphan process.
    # (For 'IBKR Recorder' this kills the python sweep loop mid-sweep; the
    # heartbeat and parquet writes are atomic/per-sweep, so nothing corrupts.)
    try { Stop-ScheduledTask -TaskPath $TaskPath -TaskName $name -ErrorAction Stop } catch { }
    Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $name -Confirm:$false
    Write-Host "Removed: $TaskPath$name"
}

# Best-effort: delete the \IBKR\ folder when nothing else lives in it.
# (No cmdlet for task folders; the Schedule.Service COM API is the only way.)
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

Write-Host "Done. Verify: Get-ScheduledTask -TaskPath '$TaskPath' (should error or list nothing)."
