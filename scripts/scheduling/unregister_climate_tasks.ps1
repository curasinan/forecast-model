# Remove the climate-label scheduled task. Run from an ELEVATED PowerShell.
Unregister-ScheduledTask -TaskPath "\Climate\" -TaskName "Climate Labels" -Confirm:$false -ErrorAction SilentlyContinue
Write-Host "Removed \Climate\Climate Labels (if it existed)."
