<#
    .SYNOPSIS
    Ограничивает доступ к настройкам rclone на аварийной станции.

    .DESCRIPTION
    В настройках rclone лежит ключ доступа к хранилищу копий склада. По
    умолчанию этот файл наследует права профиля, и на живой Windows его читали
    группы «Пользователи» и «Прошедшие проверку»: то есть любая учётная запись
    на компьютере.

    Команда снимает наследование и оставляет доступ ровно четырём: владельцу
    файла, учётной записи задания обновления копии, СИСТЕМЕ и администраторам.
    Обновление копии от этого не ломается: задание работает как раз от той
    учётной записи, что настраивала rclone.

    Содержимое файла не читается и никуда не копируется.

    Выполняется по явному решению администратора, а не сама при установке:
    слишком узкие права сломали бы ежедневное обновление молча, а это ровно тот
    отказ, которого мы избегаем.

    .EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File Protect-DenisStockEmergencyCredentials.ps1 -WhatIf

    Показывает, что будет сделано, ничего не меняя.
#>
[CmdletBinding()]
param(
    # Путь к настройкам rclone. По умолчанию берётся из конфигурации станции,
    # а если её нет - из обычного места профиля.
    [string]$ConfigPath = "",
    [string]$RepoRoot = "",
    # Учётная запись задания обновления копии. По умолчанию определяется из
    # самого задания, если оно уже создано.
    [string]$TaskAccount = "",
    [switch]$WhatIf
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
. (Join-Path $scriptDir "EmergencyBackupSource.ps1")

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent (Split-Path -Parent $scriptDir) }

if (-not $ConfigPath) {
    $envFile = Join-Path $RepoRoot ".env.emergency"
    if (Test-Path -LiteralPath $envFile) {
        foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
            if ($line -match "^\s*DENSTOCK_EMERGENCY_RCLONE_CONFIG\s*=\s*(.+)$") {
                $ConfigPath = $Matches[1].Trim()
                break
            }
        }
    }
}
if (-not $ConfigPath) { $ConfigPath = (Get-RcloneConfigPath).Path }

if (-not $TaskAccount) {
    $task = Get-ScheduledTask -TaskName "DenisStock Emergency Standby Refresh" -ErrorAction SilentlyContinue
    if ($task) { $TaskAccount = [string]$task.Principal.UserId }
}

Write-Host ""
Write-Host "Ограничение доступа к настройкам rclone" -ForegroundColor Cyan
Write-Host "  файл:            $ConfigPath" -ForegroundColor DarkGray
if ($TaskAccount) { Write-Host "  задание от:      $TaskAccount" -ForegroundColor DarkGray }
Write-Host ""

try {
    $result = Protect-RcloneConfig -Path $ConfigPath -TaskAccount $TaskAccount -WhatIf:$WhatIf
}
catch {
    Write-Host "[ОСТАНОВКА] $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "Действие: настройте rclone под этой учётной записью и повторите." -ForegroundColor Yellow
    exit 1
}

Write-Host "Читатели до:" -ForegroundColor DarkGray
foreach ($identity in $result.Before) { Write-Host "  $identity" }
Write-Host ""
if ($WhatIf) {
    Write-Host "Останутся (ничего не изменено):" -ForegroundColor Cyan
    foreach ($identity in $result.After) { Write-Host "  $identity" }
    Write-Host ""
    Write-Host "Это предварительный показ. Повторите без -WhatIf, чтобы применить." -ForegroundColor Yellow
    exit 0
}
if (-not $result.Changed) {
    Write-Host "Доступ уже ограничен, менять нечего." -ForegroundColor Green
    exit 0
}
Write-Host "Читатели после:" -ForegroundColor Cyan
foreach ($identity in $result.After) { Write-Host "  $identity" }
Write-Host ""
Write-Host "Готово. Проверьте обновление копии: DenisStock-Emergency.ps1 -Action Sync" -ForegroundColor Green
exit 0
