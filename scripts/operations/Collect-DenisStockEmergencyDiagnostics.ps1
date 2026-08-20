<#
    .SYNOPSIS
    Собирает небольшой безопасный набор сведений для разработчика.

    .DESCRIPTION
    Нужен, когда станция ведёт себя не так и по телефону не разобраться.
    Собирает версии, состояние слоёв, последние строки журналов и имена
    настроек. Значения настроек не собираются вовсе: среди них пароль базы и
    ключ Django. Данные склада, клиентов и содержимое ключей не попадают в
    набор ни при каких условиях.

    Результат: один zip-файл на рабочем столе, который можно отправить.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$OutputDirectory = "",
    [int]$LogLines = 200
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

if (-not $RepoRoot) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $here)
}
if (-not $OutputDirectory) { $OutputDirectory = [Environment]::GetFolderPath("Desktop") }

# Имена настроек, значения которых безопасно показывать. Всё остальное
# записывается как «имя есть, значение скрыто»: список секретов может
# пополниться, и разрешительный подход надёжнее запретительного.
$SafeSettingNames = @(
    "DENSTOCK_MODE", "DENSTOCK_INSTANCE_ID", "DENSTOCK_EMERGENCY_ROLE",
    "DENSTOCK_EMERGENCY_RUNTIME", "DENSTOCK_EMERGENCY_WSL_DISTRO",
    "DENSTOCK_EMERGENCY_BIND_HOST", "DENSTOCK_EMERGENCY_PORT",
    "DENSTOCK_EMERGENCY_STALE_WARNING_HOURS", "DENSTOCK_EMERGENCY_KEEP_STANDBY",
    "DENSTOCK_EMERGENCY_KEEP_DOWNLOADS", "DENSTOCK_EMERGENCY_KEEP_COMPLETED_EXPORTS",
    "DENSTOCK_APP_COMMIT", "DENSTOCK_MANIFEST_SIGNING_KEY_ID",
    "DENSTOCK_EMERGENCY_DB_PREFIX", "DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS",
    "DENSTOCK_PRODUCTION_DB_HOSTS", "DENSTOCK_PRODUCTION_URL",
    "DJANGO_SETTINGS_MODULE", "DJANGO_DEBUG", "DJANGO_ALLOWED_HOSTS",
    "DJANGO_SECURE_COOKIES", "TIME_ZONE", "POSTGRES_DB", "POSTGRES_USER",
    "AI_SUPPORT_ENABLED", "AI_SUPPORT_PROVIDER", "COMPOSE_FILE"
)

$stamp = (Get-Date).ToString("yyyyMMdd-HHmmss")
$staging = Join-Path ([IO.Path]::GetTempPath()) "denstock-diag-$stamp"
New-Item -ItemType Directory -Force -Path $staging | Out-Null

function Write-Section {
    param([string]$FileName, [string[]]$Lines)
    $Lines | Set-Content -LiteralPath (Join-Path $staging $FileName) -Encoding UTF8
}

# --- Окружение Windows -------------------------------------------------------
$os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
$cs = Get-CimInstance Win32_ComputerSystem -ErrorAction SilentlyContinue
$lines = @(
    "collected_at=$((Get-Date).ToString('o'))",
    "windows=$(if ($os) { $os.Caption.Trim() } else { 'неизвестно' })",
    "build=$(if ($os) { $os.BuildNumber } else { '' })",
    "architecture=$([Environment]::Is64BitOperatingSystem)",
    "memory_gb=$(if ($cs) { [math]::Round($cs.TotalPhysicalMemory / 1GB, 1) } else { '' })",
    "powershell=$($PSVersionTable.PSVersion)",
    "repo_root=$RepoRoot"
)
Write-Section -FileName "01-windows.txt" -Lines $lines

# --- Слои станции ------------------------------------------------------------
$diagnostics = Join-Path $PSScriptRoot "Test-DenisStockEmergency.ps1"
if (Test-Path -LiteralPath $diagnostics) {
    $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $diagnostics -RepoRoot $RepoRoot -Quiet 2>&1
    Write-Section -FileName "02-layers.txt" -Lines @($output | Out-String -Stream)
}

# --- Настройки: только имена, значения скрыты ---------------------------------
$envFile = Join-Path $RepoRoot ".env.emergency"
$settingLines = @()
if (Test-Path -LiteralPath $envFile) {
    foreach ($line in Get-Content -LiteralPath $envFile -Encoding UTF8) {
        if ($line -notmatch "^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$") { continue }
        $name = $Matches[1]
        if ($SafeSettingNames -contains $name) { $settingLines += "$name=$($Matches[2].Trim())" }
        else { $settingLines += "$name=<скрыто>" }
    }
}
else { $settingLines += "нет .env.emergency" }
Write-Section -FileName "03-settings.txt" -Lines $settingLines

# --- Идентификатор и доверенный ключ -------------------------------------------
$runtimeRoot = Join-Path $RepoRoot ".emergency"
$identityFile = Join-Path $runtimeRoot "workstation-id.txt"
$pinnedKey = Join-Path $runtimeRoot "trusted\production-manifest-ed25519-public.pem"
$identityLines = @()
if (Test-Path -LiteralPath $identityFile) {
    $identityLines += "workstation_id=$((Get-Content -LiteralPath $identityFile -Raw -Encoding UTF8).Trim())"
}
else { $identityLines += "workstation_id=<не создан>" }
if (Test-Path -LiteralPath $pinnedKey) {
    # В набор кладётся только отпечаток. Сам файл публичный, но он здесь не
    # нужен, а привычка не копировать ключи в архивы полезнее исключения.
    $pem = Get-Content -LiteralPath $pinnedKey -Raw
    $base64 = ($pem -replace "-----BEGIN PUBLIC KEY-----", "" `
                    -replace "-----END PUBLIC KEY-----", "" -replace "\s", "")
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $identityLines += "pinned_public_key_fingerprint=" +
            (([BitConverter]::ToString($sha.ComputeHash([Convert]::FromBase64String($base64))) -replace "-", "").ToLower())
    }
    catch { $identityLines += "pinned_public_key_fingerprint=<файл не читается>" }
    finally { $sha.Dispose() }
}
else { $identityLines += "pinned_public_key_fingerprint=<не закреплён>" }
Write-Section -FileName "04-identity.txt" -Lines $identityLines

# --- Журналы контейнеров -------------------------------------------------------
$distro = ($settingLines | Where-Object { $_ -like "DENSTOCK_EMERGENCY_WSL_DISTRO=*" } |
    ForEach-Object { $_.Split("=", 2)[1] } | Select-Object -First 1)
if (-not $distro) { $distro = "Ubuntu" }
& wsl.exe -d $distro -- docker info *> $null
if ($LASTEXITCODE -eq 0) {
    $containers = & wsl.exe -d $distro -- docker ps -a --format "{{.Names}}`t{{.Status}}" 2>&1
    Write-Section -FileName "05-containers.txt" -Lines @($containers | Out-String -Stream)
    foreach ($name in @("emergency-web", "emergency-db")) {
        $log = & wsl.exe -d $distro -- docker logs --tail $LogLines $name 2>&1
        Write-Section -FileName "06-log-$name.txt" -Lines @($log | Out-String -Stream)
    }
}
else {
    Write-Section -FileName "05-containers.txt" -Lines @("Docker недоступен, журналы не собраны")
}

# --- Состояние обновления standby ------------------------------------------------
$refreshStatus = Join-Path $runtimeRoot "standby-refresh-status.json"
if (Test-Path -LiteralPath $refreshStatus) {
    Copy-Item -LiteralPath $refreshStatus -Destination (Join-Path $staging "07-standby-refresh.json")
}

# --- Проверка: в наборе не должно быть секретов -------------------------------
$leaks = @()
foreach ($file in Get-ChildItem -LiteralPath $staging -File) {
    $text = Get-Content -LiteralPath $file.FullName -Raw -ErrorAction SilentlyContinue
    if (-not $text) { continue }
    foreach ($pattern in @("BEGIN PRIVATE KEY", "BEGIN OPENSSH PRIVATE KEY", "POSTGRES_PASSWORD=[^<]",
                           "DJANGO_SECRET_KEY=[^<]", "DENSTOCK_EMERGENCY_PROBE_TOKEN=[^<]", "DATABASE_URL=[^<]")) {
        if ($text -match $pattern) { $leaks += "$($file.Name): $pattern" }
    }
}
if ($leaks.Count -gt 0) {
    Remove-Item -LiteralPath $staging -Recurse -Force
    throw "Сбор остановлен: в набор попали секреты ($($leaks -join '; ')). Архив не создан."
}

$archive = Join-Path $OutputDirectory "denstock-emergency-diagnostics-$stamp.zip"
Compress-Archive -Path (Join-Path $staging "*") -DestinationPath $archive -Force
Remove-Item -LiteralPath $staging -Recurse -Force

Write-Host ""
Write-Host "Набор диагностики собран:" -ForegroundColor Green
Write-Host "  $archive"
Write-Host "Секретов и данных склада в нём нет, файл можно отправить разработчику." -ForegroundColor DarkGray
