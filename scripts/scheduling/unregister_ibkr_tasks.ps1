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

Write-Host "Done. Verify: Get-ScheduledTask -TaskPath '$TaskPath' (should error or list nothing)."
