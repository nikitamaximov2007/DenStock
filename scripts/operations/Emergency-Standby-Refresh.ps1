$ErrorActionPreference = "Stop"
$controller = Join-Path $PSScriptRoot "DenisStock-Emergency.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $controller -Action Sync -NonInteractive
exit $LASTEXITCODE
