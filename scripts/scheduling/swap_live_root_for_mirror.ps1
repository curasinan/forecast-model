# Turn this machine's live quant-data root into the kept root and put an empty,
# marked mirror in its place -- or, with -Undo, put the kept root back.
#
# Run in PowerShell from the repo root. Forward: at the cutover (plan Task 12),
# only after the Zenbook's Gateway is closed, its four scheduled tasks are
# disabled and the final push has been verified. -Undo: at a rollback, after
# scripts/spark/rollback.sh has ended with exit 0.
#
#   .\scripts\scheduling\swap_live_root_for_mirror.ps1          # quant_data -> quant_data_zenbook_final; a new quant_data with the marker
#   .\scripts\scheduling\swap_live_root_for_mirror.ps1 -Undo    # the mirror -> quant_data_mirror_retired; the kept root -> quant_data
#
# Every step is checked and the first failure ends the script with exit 1 and a
# line that starts "swap refused:". The reason this is a script and not four
# pasted lines: a rename that Windows refuses (some program holds a file open
# inside the root) is a non-terminating error, and the lines after it would
# then create the marker file inside the LIVE root. The pull would take that
# root for a mirror and write into it, and the rollback would have no kept
# root to go back to. Close anything that has a file open under the root first.
param(
    [switch]$Undo,
    [string]$UserHome = $env:USERPROFILE
)

$ErrorActionPreference = 'Stop'
$marker  = '.mirror-of-spark'
$root    = Join-Path $UserHome 'quant_data'
$kept    = Join-Path $UserHome 'quant_data_zenbook_final'
$retired = Join-Path $UserHome 'quant_data_mirror_retired'

function Stop-Swap([string]$message) {
    Write-Host "swap refused: $message"
    exit 1
}

if (-not $Undo) {
    if (-not (Test-Path $root -PathType Container)) { Stop-Swap "$root does not exist" }
    if (Test-Path (Join-Path $root $marker))        { Stop-Swap "$root already carries $marker, so it is a mirror already" }
    if (Test-Path $kept)                            { Stop-Swap "$kept already exists" }
    try {
        Rename-Item -Path $root -NewName 'quant_data_zenbook_final'
    } catch {
        Stop-Swap "Windows would not rename $root ($($_.Exception.Message)); nothing was changed"
    }
    if ((Test-Path $root) -or -not (Test-Path $kept -PathType Container)) {
        Stop-Swap "the rename did not take effect; look at $UserHome before doing anything else"
    }
    New-Item -ItemType Directory -Path $root | Out-Null
    Set-Content -Path (Join-Path $root $marker) -Value ("mirror of spark:/srv/quant_data, created {0:s}" -f (Get-Date))
    Write-Host "kept root:  $kept"
    Write-Host "new mirror: $root (empty, carries $marker)"
    exit 0
}

if (-not (Test-Path $kept -PathType Container)) { Stop-Swap "$kept does not exist, so there is no kept root to put back" }
if (Test-Path (Join-Path $kept $marker))         { Stop-Swap "$kept carries $marker, so it is not the kept live root" }
if (Test-Path $retired)                          { Stop-Swap "$retired already exists" }
if (Test-Path $root) {
    if (-not (Test-Path (Join-Path $root $marker))) {
        Stop-Swap "$root does not carry $marker, so it is not the mirror; nothing was changed"
    }
    try {
        Rename-Item -Path $root -NewName 'quant_data_mirror_retired'
    } catch {
        Stop-Swap "Windows would not rename the mirror ($($_.Exception.Message)); nothing was changed"
    }
    if (Test-Path $root) { Stop-Swap "the mirror is still at $root; nothing else was changed" }
}
try {
    Rename-Item -Path $kept -NewName 'quant_data'
} catch {
    Stop-Swap "Windows would not rename the kept root back ($($_.Exception.Message)); the mirror is now at $retired"
}
Write-Host "live root:      $root"
Write-Host "retired mirror: $retired"
exit 0
