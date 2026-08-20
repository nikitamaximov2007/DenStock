<#
    .SYNOPSIS
    Диагностика аварийной станции по слоям, снизу вверх.

    .DESCRIPTION
    Обычный статус спрашивает само приложение и потому замолкает, когда сломан
    слой под ним: не поднялся WSL, не запущен Docker, нет контейнера. Здесь
    каждый слой проверяется отдельно, и проверка не останавливается на первом
    отказе. Человек видит, какой именно слой виноват.

    Секретов не печатает: ни паролей, ни содержимого ключей, ни данных склада.

    Код возврата: 0, если станция готова; 1, если нет.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = "",
    [string]$WslDistro = "",
    # Источник копий. По умолчанию берётся из настроек станции, а до установки
    # его можно передать вручную: проверить источник надо ДО установки, иначе
    # станция встанет готовой, но без чего забирать копии.
    [string]$BackupSource = "",
    [switch]$SkipBackupSource,
    [switch]$Quiet
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

# $PSScriptRoot не заполнен в значениях параметров по умолчанию, поэтому корень
# репозитория вычисляется здесь: сценарий лежит в scripts/operations.
if (-not $RepoRoot) {
    $here = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
    $RepoRoot = Split-Path -Parent (Split-Path -Parent $here)
}
if (-not $RepoRoot) { $RepoRoot = (Get-Location).Path }

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$helper = Join-Path $scriptDir "EmergencyBackupSource.ps1"
if (Test-Path -LiteralPath $helper) { . $helper }

$script:Layers = @()
$script:NextSteps = @()

function Add-NextStep {
    param([string]$Text)
    if ($Text -and $script:NextSteps -notcontains $Text) { $script:NextSteps += $Text }
}

function Add-Layer {
    param(
        [Parameter(Mandatory)][string]$Layer,
        [Parameter(Mandatory)][ValidateSet("ГОТОВО", "ВНИМАНИЕ", "ОШИБКА", "НЕТ ДАННЫХ")][string]$State,
        [string]$Detail = ""
    )
    $script:Layers += [pscustomobject]@{ Layer = $Layer; State = $State; Detail = $Detail }
}

function Get-EmergencyEnvValue {
    <#
        Читает одно значение из .env.emergency. Возвращает только те ключи, у
        которых значение не является секретом; вызывающая сторона обязана
        передавать безопасные имена.
    #>
    param([string]$Path, [string]$Name)
    if (-not (Test-Path -LiteralPath $Path)) { return "" }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*)$") { return $Matches[1].Trim() }
    }
    return ""
}

$envFile = Join-Path $RepoRoot ".env.emergency"
$runtimeRoot = Join-Path $RepoRoot ".emergency"
$identityFile = Join-Path $runtimeRoot "workstation-id.txt"
$pinnedKey = Join-Path $runtimeRoot "trusted\production-manifest-ed25519-public.pem"

if (-not $WslDistro) {
    $WslDistro = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_WSL_DISTRO"
    if (-not $WslDistro) { $WslDistro = "Ubuntu" }
}

# --- Слой 1: Windows -----------------------------------------------------------
try {
    $os = Get-CimInstance Win32_OperatingSystem
    Add-Layer -Layer "Windows" -State "ГОТОВО" -Detail "$($os.Caption.Trim()) (сборка $($os.BuildNumber))"
}
catch { Add-Layer -Layer "Windows" -State "ОШИБКА" -Detail $_.Exception.Message }

# --- Слой 2: WSL ----------------------------------------------------------------
$wslReady = $false
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    Add-Layer -Layer "Подсистема Linux" -State "ОШИБКА" -Detail "wsl.exe не найден"
}
else {
    # Вызов ограничен по времени. На машине со сломанной подсистемой Linux
    # wsl.exe -l -q возвращался четыре минуты, и проверка готовности молчала
    # всё это время. Просроченный вызов честнее ответа через четыре минуты.
    $listing = Invoke-ExternalWithTimeout -FilePath "wsl.exe" -Arguments @("-l", "-q") `
        -TimeoutSeconds 25 -Utf16Output
    if ($listing.TimedOut) {
        Add-Layer -Layer "Подсистема Linux" -State "ОШИБКА" `
            -Detail "wsl.exe не ответил за 25 секунд"
        Add-NextStep "Подсистема Linux не отвечает. Выполните wsl --shutdown, затем wsl --update, и повторите проверку."
    }
    elseif ($listing.ExitCode -ne 0) {
        Add-Layer -Layer "Подсистема Linux" -State "ОШИБКА" -Detail $listing.Text.Trim()
    }
    else {
        $names = @(
            $listing.Text -split "`n" |
                ForEach-Object { $_.Replace([string][char]0, "").Trim() } |
                Where-Object { $_ }
        )
        if ($names -contains $WslDistro) {
            Add-Layer -Layer "Подсистема Linux" -State "ГОТОВО" -Detail "дистрибутив $WslDistro"
            $wslReady = $true
        }
        else {
            Add-Layer -Layer "Подсистема Linux" -State "ОШИБКА" -Detail "нет дистрибутива $WslDistro"
        }
    }
}

# --- Слой 3: Docker -------------------------------------------------------------
$dockerReady = $false
if (-not $wslReady) {
    Add-Layer -Layer "Docker" -State "НЕТ ДАННЫХ" -Detail "не проверялся: не работает подсистема Linux"
}
else {
    $probe = Invoke-ExternalWithTimeout -FilePath "wsl.exe" `
        -Arguments @("-d", $WslDistro, "--", "docker", "info") -TimeoutSeconds 40
    if ($probe.TimedOut) {
        Add-Layer -Layer "Docker" -State "ОШИБКА" -Detail "docker не ответил за 40 секунд"
        Add-NextStep "Docker не отвечает. Выполните: wsl -d $WslDistro -u root -- systemctl start docker"
    }
    elseif ($probe.ExitCode -eq 0) {
        $versionProbe = Invoke-ExternalWithTimeout -FilePath "wsl.exe" `
            -Arguments @("-d", $WslDistro, "--", "docker", "--version") -TimeoutSeconds 20
        $version = if ($versionProbe.TimedOut) { "версия не получена" } else { $versionProbe.Text.Trim() }
        Add-Layer -Layer "Docker" -State "ГОТОВО" -Detail $version
        $dockerReady = $true
    }
    else {
        Add-Layer -Layer "Docker" -State "ОШИБКА" `
            -Detail "Docker в $WslDistro не отвечает. Запустите: wsl -d $WslDistro -u root -- systemctl start docker"
    }
}

# --- Слой 4: конфигурация станции ------------------------------------------------
if (Test-Path -LiteralPath $envFile) {
    $mode = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_MODE"
    $role = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_ROLE"
    $bind = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_BIND_HOST"
    $port = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_PORT"
    Add-Layer -Layer "Конфигурация" -State "ГОТОВО" -Detail "режим $mode, роль $role, адрес $bind`:$port"
}
else {
    Add-Layer -Layer "Конфигурация" -State "ОШИБКА" -Detail "нет .env.emergency: станция не установлена"
}

# --- Слой 5: идентификатор станции ------------------------------------------------
if (Test-Path -LiteralPath $identityFile) {
    $id = (Get-Content -LiteralPath $identityFile -Raw -Encoding UTF8).Trim()
    $valid = $false
    try { [void][Guid]::Parse($id); $valid = $true } catch { $valid = $false }
    if ($valid) { Add-Layer -Layer "Идентификатор станции" -State "ГОТОВО" -Detail $id }
    else { Add-Layer -Layer "Идентификатор станции" -State "ОШИБКА" -Detail "файл повреждён" }
}
else {
    Add-Layer -Layer "Идентификатор станции" -State "ОШИБКА" -Detail "не создан"
}

# --- Слой 6: закреплённый публичный ключ -------------------------------------------
if (Test-Path -LiteralPath $pinnedKey) {
    try {
        $pem = Get-Content -LiteralPath $pinnedKey -Raw
        $base64 = ($pem -replace "-----BEGIN PUBLIC KEY-----", "" `
                        -replace "-----END PUBLIC KEY-----", "" -replace "\s", "")
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $fingerprint = ([BitConverter]::ToString($sha.ComputeHash([Convert]::FromBase64String($base64))) `
                -replace "-", "").ToLower()
        }
        finally { $sha.Dispose() }
        $keyId = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_MANIFEST_SIGNING_KEY_ID"
        Add-Layer -Layer "Доверенный ключ" -State "ГОТОВО" -Detail "$keyId, отпечаток $fingerprint"
    }
    catch { Add-Layer -Layer "Доверенный ключ" -State "ОШИБКА" -Detail "файл не читается как PEM" }
}
else {
    Add-Layer -Layer "Доверенный ключ" -State "ОШИБКА" -Detail "не закреплён"
}

# --- Слой 7: сеть ------------------------------------------------------------------
$bindHost = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_BIND_HOST"
$bindPort = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_PORT"
if ($bindHost -and $bindHost -ne "127.0.0.1") {
    $owns = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $bindHost }
    if ($owns) {
        $rule = Get-NetFirewallRule -DisplayName "DenisStock Emergency LAN $bindPort" -ErrorAction SilentlyContinue
        if ($rule) { Add-Layer -Layer "Сеть" -State "ГОТОВО" -Detail "адрес $bindHost, правило брандмауэра есть" }
        else { Add-Layer -Layer "Сеть" -State "ВНИМАНИЕ" -Detail "адрес $bindHost, правила брандмауэра нет" }
    }
    else {
        Add-Layer -Layer "Сеть" -State "ОШИБКА" `
            -Detail "адрес $bindHost больше не принадлежит компьютеру: ярлыки на других компьютерах не откроются"
    }
}
elseif ($bindHost) {
    Add-Layer -Layer "Сеть" -State "ГОТОВО" -Detail "только этот компьютер ($bindHost)"
}
else {
    Add-Layer -Layer "Сеть" -State "НЕТ ДАННЫХ" -Detail "адрес не настроен"
}

# --- Слой 8: само приложение ---------------------------------------------------------
$appReady = $false
if (-not $dockerReady) {
    Add-Layer -Layer "DenisStock" -State "НЕТ ДАННЫХ" -Detail "не проверялся: не работает Docker"
}
else {
    $launcher = Join-Path $PSScriptRoot "DenisStock-Emergency.ps1"
    if (-not (Test-Path -LiteralPath $launcher)) {
        Add-Layer -Layer "DenisStock" -State "ОШИБКА" -Detail "не найден DenisStock-Emergency.ps1"
    }
    else {
        $output = & powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher -Action Status 2>&1
        $statusCode = $LASTEXITCODE
        $text = ($output | Out-String)
        if ($statusCode -eq 0) {
            Add-Layer -Layer "DenisStock" -State "ГОТОВО" -Detail "статус получен"
            $appReady = $true
        }
        else {
            $firstLine = ($text -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
            Add-Layer -Layer "DenisStock" -State "ОШИБКА" -Detail $firstLine.Trim()
        }
        if (-not $Quiet -and $text.Trim()) {
            $script:AppStatusText = $text.Trim()
        }
    }
}

# --- Слой 9: rclone и источник копий -------------------------------------------------
if (-not $BackupSource) {
    $BackupSource = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_BACKUP_SOURCE"
}
if ($SkipBackupSource) {
    Add-Layer -Layer "Источник копий" -State "НЕТ ДАННЫХ" -Detail "проверка пропущена по просьбе оператора"
}
elseif (-not (Get-Command Test-EmergencyBackupSource -ErrorAction SilentlyContinue)) {
    Add-Layer -Layer "Источник копий" -State "НЕТ ДАННЫХ" -Detail "не найден EmergencyBackupSource.ps1"
}
elseif (-not $BackupSource) {
    Add-Layer -Layer "Источник копий" -State "ОШИБКА" -Detail "не задан"
    Add-NextStep "Указать источник копий: параметр -BackupSource или установка станции."
}
else {
    $probe = Test-EmergencyBackupSource -Source $BackupSource
    if ($probe.RcloneVersion) {
        Add-Layer -Layer "rclone" -State "ГОТОВО" -Detail $probe.RcloneVersion
    }
    elseif ($probe.Kind -eq "not-installed") {
        Add-Layer -Layer "rclone" -State "ОШИБКА" -Detail "не установлен"
    }

    if ($probe.State -eq "ГОТОВО") {
        Add-Layer -Layer "Источник копий" -State "ГОТОВО" -Detail "$($probe.Remote): $($probe.Detail)"
        $age = Get-BackupRunAgeHours -RunId $probe.LatestRun
        $staleHours = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_STALE_WARNING_HOURS"
        if (-not $staleHours) { $staleHours = "24" }
        if ($null -eq $age) {
            Add-Layer -Layer "Свежесть копии" -State "ВНИМАНИЕ" `
                -Detail "имя копии $($probe.LatestRun) не разобрано как дата"
        }
        elseif ($age -gt [double]$staleHours) {
            Add-Layer -Layer "Свежесть копии" -State "ВНИМАНИЕ" `
                -Detail "последняя копия старше $staleHours ч: $($probe.LatestRun), возраст $age ч"
            Add-NextStep "Проверить, что production создаёт копии и выгружает их в хранилище."
        }
        else {
            Add-Layer -Layer "Свежесть копии" -State "ГОТОВО" `
                -Detail "$($probe.LatestRun), возраст $age ч"
        }
    }
    else {
        Add-Layer -Layer "Источник копий" -State "ОШИБКА" -Detail "$($probe.Kind): $($probe.Detail)"
        Add-NextStep $probe.Advice
    }
}

# --- Слой 10: задание обновления копии -------------------------------------------------
$task = Get-ScheduledTask -TaskName "DenisStock Emergency Standby Refresh" -ErrorAction SilentlyContinue
if ($task) {
    Add-Layer -Layer "Задание обновления" -State "ГОТОВО" -Detail "состояние: $($task.State)"
}
elseif (Test-Path -LiteralPath $envFile) {
    Add-Layer -Layer "Задание обновления" -State "ВНИМАНИЕ" -Detail "не создано"
    Add-NextStep "Повторить установку с ключом -CreateTasks, чтобы копия обновлялась сама."
}
else {
    Add-Layer -Layer "Задание обновления" -State "НЕТ ДАННЫХ" -Detail "станция ещё не установлена"
}

# --- Слой 11: контекст задания и настройки rclone -------------------------------------
# Обновление копии выполняет rclone, а его настройки лежат в профиле того
# пользователя Windows, который их создавал. Если задание пойдёт под другим
# пользователем или под СИСТЕМОЙ, источник копий просто не найдётся, и станция
# начнёт устаревать молча. Поэтому контекст проверяется отдельно.
if (Get-Command Get-RcloneConfigPath -ErrorAction SilentlyContinue) {
    $conf = Get-RcloneConfigPath
    if (-not $conf.Exists) {
        Add-Layer -Layer "Настройки rclone" -State "ОШИБКА" -Detail "нет файла: $($conf.Path)"
        Add-NextStep "Выполнить rclone config под тем пользователем Windows, от которого работает станция."
    }
    elseif ($conf.ExtraReaders.Count -gt 0) {
        Add-Layer -Layer "Настройки rclone" -State "ВНИМАНИЕ" `
            -Detail "$($conf.Path); лишние читатели: $($conf.ExtraReaders -join ', ')"
        Add-NextStep "Ограничить доступ к настройкам rclone: ключ к хранилищу копий не должен читаться посторонними учётными записями."
    }
    else {
        Add-Layer -Layer "Настройки rclone" -State "ГОТОВО" -Detail $conf.Path
    }
}

$refreshTask = Get-ScheduledTask -TaskName "DenisStock Emergency Standby Refresh" -ErrorAction SilentlyContinue
if ($refreshTask) {
    $account = [string]$refreshTask.Principal.UserId
    $logon = [string]$refreshTask.Principal.LogonType
    $me = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $shortMe = $me.Split("\\")[-1]
    $detail = "учётная запись $account, вход $logon"
    if ($account -match "^(SYSTEM|NT AUTHORITY)" -or $logon -eq "ServiceAccount") {
        Add-Layer -Layer "Контекст задания" -State "ОШИБКА" -Detail $detail
        Add-NextStep "Задание идёт под системной учётной записью: у неё другой профиль и она не увидит настройки rclone. Повторите установку с ключом -CreateTasks."
    }
    elseif ($logon -eq "Interactive") {
        Add-Layer -Layer "Контекст задания" -State "ВНИМАНИЕ" -Detail $detail
        Add-NextStep "Задание сработает только когда этот пользователь в системе. Повторите установку с ключом -CreateTasks, чтобы копия обновлялась и при заблокированном экране."
    }
    elseif ($account -ne $me -and $account -ne $shortMe) {
        Add-Layer -Layer "Контекст задания" -State "ВНИМАНИЕ" `
            -Detail "$detail; проверка идёт от $me"
        Add-NextStep "Проверьте источник копий под учётной записью ${account}: настройки rclone лежат в её профиле."
    }
    else {
        Add-Layer -Layer "Контекст задания" -State "ГОТОВО" -Detail $detail
    }
}

# --- Слой 12: последнее обновление копии ------------------------------------------------
$refreshStatusFile = Join-Path $runtimeRoot "standby-refresh-status.json"
if (Test-Path -LiteralPath $refreshStatusFile -PathType Leaf) {
    try {
        $status = Get-Content -LiteralPath $refreshStatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
        $when = [DateTimeOffset]::Parse($status.attempted_at).LocalDateTime.ToString("yyyy-MM-dd HH:mm")
        $detail = "$when, итог: $($status.outcome), копия $($status.backup_run_id)"
        if ($status.outcome -eq "ready") {
            Add-Layer -Layer "Последнее обновление" -State "ГОТОВО" -Detail $detail
        }
        elseif ($status.outcome -eq "incompatible") {
            Add-Layer -Layer "Последнее обновление" -State "ВНИМАНИЕ" -Detail $detail
            Add-NextStep "Версия станции не совпала с копией. Обновите станцию до версии production и повторите обновление копии."
        }
        else {
            Add-Layer -Layer "Последнее обновление" -State "ВНИМАНИЕ" -Detail $detail
            Add-NextStep "Последнее обновление копии не удалось. Прежняя рабочая копия сохранена; проверьте источник копий."
        }
    }
    catch {
        Add-Layer -Layer "Последнее обновление" -State "ВНИМАНИЕ" -Detail "запись о последнем обновлении не читается"
    }
}
elseif (Test-Path -LiteralPath $envFile) {
    Add-Layer -Layer "Последнее обновление" -State "НЕТ ДАННЫХ" -Detail "копия ещё ни разу не обновлялась"
}

# --- Слой 13: источник выпуска ---------------------------------------------------------
# Ветка может выглядеть опубликованной локально, а на сервере её не быть.
# Обнаружилось бы это в худший момент: пришла копия с новой версией, станция
# пытается обновиться и не может.
if (Get-Command Test-EmergencyReleaseSource -ErrorAction SilentlyContinue) {
    $releaseSource = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_EMERGENCY_RELEASE_SOURCE"
    $releaseCommit = Get-EmergencyEnvValue -Path $envFile -Name "DENSTOCK_APP_COMMIT"
    if ($releaseSource -and $releaseCommit) {
        $release = Test-EmergencyReleaseSource -Source $releaseSource -Commit $releaseCommit `
            -RepoRoot $RepoRoot
        if ($release.State -eq "ГОТОВО") {
            Add-Layer -Layer "Источник выпуска" -State "ГОТОВО" -Detail $release.Detail
        }
        else {
            Add-Layer -Layer "Источник выпуска" -State "ВНИМАНИЕ" `
                -Detail "$($release.Kind): $($release.Detail)"
            Add-NextStep $release.Advice
        }
    }
    elseif (Test-Path -LiteralPath $envFile) {
        Add-Layer -Layer "Источник выпуска" -State "ВНИМАНИЕ" -Detail "не настроен"
        Add-NextStep "Без источника выпуска станция не сможет перейти на новую версию, когда придёт копия с ней."
    }
}

# --- Отчёт ---------------------------------------------------------------------------
Write-Host ""
Write-Host "Диагностика аварийной станции DenisStock" -ForegroundColor Cyan
Write-Host ""
$width = ($script:Layers | ForEach-Object { $_.Layer.Length } | Measure-Object -Maximum).Maximum
foreach ($layer in $script:Layers) {
    $color = switch ($layer.State) {
        "ГОТОВО" { "Green" }
        "ВНИМАНИЕ" { "Yellow" }
        "НЕТ ДАННЫХ" { "DarkGray" }
        default { "Red" }
    }
    Write-Host ("  {0}  " -f $layer.Layer.PadRight($width)) -NoNewline
    Write-Host $layer.State.PadRight(12) -ForegroundColor $color -NoNewline
    Write-Host $layer.Detail -ForegroundColor DarkGray
}

if (-not $Quiet -and (Get-Variable -Name AppStatusText -Scope Script -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "Состояние аварийного режима:" -ForegroundColor Cyan
    Write-Host $script:AppStatusText
}

$broken = @($script:Layers | Where-Object { $_.State -eq "ОШИБКА" })
$installed = Test-Path -LiteralPath $envFile

# Пока станция не установлена, отсутствие её частей - это не поломка, а
# незавершённая установка. Разница важна: иначе человек после первого же
# запуска видит красный список и думает, что всё сломано.
if (-not $installed) {
    Add-NextStep "Запустить установку: Install-DenisStock-EmergencyWorkstation.ps1 (см. emergency-install-kit.md)."
}
elseif (-not $wslReady) {
    Add-NextStep "Подготовить подсистему Linux и Docker: установщик с ключом -InstallWslRuntime, затем перезагрузка."
}
elseif (-not $dockerReady) {
    Add-NextStep "Запустить Docker: wsl -d $WslDistro -u root -- systemctl start docker"
}
elseif (-not $appReady) {
    Add-NextStep "Получить копию склада: DenisStock-Emergency.ps1 -Action Sync"
}

if ($script:NextSteps.Count -gt 0) {
    Write-Host ""
    Write-Host "Что делать дальше:" -ForegroundColor Cyan
    $index = 1
    foreach ($step in $script:NextSteps) {
        Write-Host ("  {0}. {1}" -f $index, $step)
        $index++
    }
}

Write-Host ""
if ($installed -and $broken.Count -eq 0 -and $appReady) {
    Write-Host "ГОТОВО: станция готова к работе." -ForegroundColor Green
    exit 0
}
if (-not $installed) {
    Write-Host "НЕ ГОТОВО: станция ещё не установлена." -ForegroundColor Yellow
    exit 1
}
$first = if ($broken.Count -gt 0) { $broken[0].Layer } else { "DenisStock" }
Write-Host "НЕ ГОТОВО. Разбирайтесь снизу вверх, начиная со слоя: $first" -ForegroundColor Red
exit 1
