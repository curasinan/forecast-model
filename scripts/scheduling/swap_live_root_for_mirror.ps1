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
$mirrorRetired = $false
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
    $mirrorRetired = $true
}
try {
    Rename-Item -Path $kept -NewName 'quant_data'
} catch {
    $reason = $_.Exception.Message
    $state = 'nothing was changed'
    if ($mirrorRetired) {
        try {
            Rename-Item -Path $retired -NewName 'quant_data'
            $state = "the mirror is back at $root; -Undo can be run again"
        } catch {
            $why = $_.Exception.Message
            $state = "the mirror could not be put back ($why) and is at $retired; " +
                     "rename it to quant_data by hand before running -Undo again"
        }
    }
    Stop-Swap "Windows would not rename the kept root back ($reason); $state"
}
Write-Host "live root:      $root"
Write-Host "retired mirror: $retired"
exit 0
