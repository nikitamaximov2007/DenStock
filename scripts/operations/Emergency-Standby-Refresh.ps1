$ErrorActionPreference = "Stop"

# Обновление копии запускается заданием планировщика с типом входа S4U. При
# таком входе полагаться на то, что Windows подставит профиль пользователя и
# переменную APPDATA, нельзя: если она не подставится, rclone не найдёт свои
# настройки, источник копий "исчезнет", и станция начнёт устаревать молча.
#
# Поэтому путь к настройкам определён при установке и записан в конфигурацию
# станции. Здесь он просто подставляется в RCLONE_CONFIG. Это путь, а не
# секрет: сам файл не читается и никуда не копируется.
$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$envFile = Join-Path $repoRoot ".env.emergency"
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -match "^\s*DENSTOCK_EMERGENCY_RCLONE_CONFIG\s*=\s*(.+)$") {
            $configured = $Matches[1].Trim()
            if ($configured) { $env:RCLONE_CONFIG = $configured }
            break
        }
    }
}

$controller = Join-Path $PSScriptRoot "DenisStock-Emergency.ps1"
& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $controller -Action Sync -NonInteractive
exit $LASTEXITCODE
