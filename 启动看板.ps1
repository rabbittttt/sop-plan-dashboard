param([switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'start-sop.ps1') @PSBoundParameters
