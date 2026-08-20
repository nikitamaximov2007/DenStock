<#
    .SYNOPSIS
    Проверка готовности компьютера к установке аварийного DenisStock.

    .DESCRIPTION
    Запускается ПЕРЕД установкой и ничего не меняет. Задача одна: сказать
    человеку, можно ли начинать, и если нет, то что именно исправить.

    Итог каждой проверки:
      ГОТОВО      - можно продолжать;
      ВНИМАНИЕ    - установка пойдёт, но что-то стоит поправить;
      ОСТАНОВКА   - устанавливать нельзя, пока не исправлено.

    Код возврата: 0, если остановок нет; 1, если есть хотя бы одна.
#>
[CmdletBinding()]
param(
    # Порт, на котором аварийная станция будет отвечать в локальной сети.
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    # Каталог, куда планируется поставить релиз. Нужен для проверки места.
    [string]$RepoRoot = "C:\DenisStock",
    [string]$WslDistro = "Ubuntu",
    # Источник копий проверяется до установки: станция без него встанет
    # готовой, но забирать копии ей будет неоткуда, и выяснится это только
    # на первой синхронизации.
    [string]$BackupSource = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Continue"

$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$helper = Join-Path $scriptDir "EmergencyBackupSource.ps1"
if (Test-Path -LiteralPath $helper) { . $helper }

$script:Results = @()

function Add-Check {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet("ГОТОВО", "ВНИМАНИЕ", "ОСТАНОВКА")][string]$State,
        [string]$Detail = "",
        [string]$Fix = ""
    )
    $script:Results += [pscustomobject]@{
        Name = $Name; State = $State; Detail = $Detail; Fix = $Fix
    }
}

function Test-Safely {
    <#
        Ни одна проверка не должна ронять весь отчёт. Сломавшаяся проверка
        честнее показывается как «внимание», чем прячет остальные.
    #>
    param([string]$Name, [scriptblock]$Body)
    try { & $Body }
    catch { Add-Check -Name $Name -State "ВНИМАНИЕ" -Detail "Проверка не выполнилась: $($_.Exception.Message)" }
}

Write-Host ""
Write-Host "Проверка компьютера перед установкой аварийного DenisStock" -ForegroundColor Cyan
Write-Host "Ничего не изменяется, только чтение." -ForegroundColor DarkGray
Write-Host ""

# --- Права ---------------------------------------------------------------------
Test-Safely "Права администратора" {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        Add-Check -Name "Права администратора" -State "ГОТОВО" -Detail $identity.Name
    }
    else {
        Add-Check -Name "Права администратора" -State "ОСТАНОВКА" `
            -Detail "PowerShell запущен без прав администратора" `
            -Fix "Закройте окно. Нажмите Пуск, наберите PowerShell, правая кнопка, «Запуск от имени администратора»."
    }
}

# --- Windows -------------------------------------------------------------------
Test-Safely "Версия Windows" {
    $os = Get-CimInstance Win32_OperatingSystem
    $build = [int]($os.BuildNumber)
    $caption = $os.Caption.Trim()
    # WSL2 поддерживается с Windows 10 build 19041. Ниже установка невозможна.
    if ($build -ge 19041) {
        Add-Check -Name "Версия Windows" -State "ГОТОВО" -Detail "$caption (сборка $build)"
    }
    else {
        Add-Check -Name "Версия Windows" -State "ОСТАНОВКА" `
            -Detail "$caption (сборка $build)" `
            -Fix "Нужна Windows 10 сборки 19041 или новее, либо Windows 11. Обновите Windows."
    }
}

Test-Safely "Разрядность" {
    if ([Environment]::Is64BitOperatingSystem) {
        Add-Check -Name "Разрядность" -State "ГОТОВО" -Detail "64-разрядная"
    }
    else {
        Add-Check -Name "Разрядность" -State "ОСТАНОВКА" -Detail "32-разрядная система" `
            -Fix "Нужен 64-разрядный компьютер. На этом установка невозможна."
    }
}

# --- Виртуализация -------------------------------------------------------------
Test-Safely "Виртуализация" {
    $cs = Get-CimInstance Win32_ComputerSystem
    $enabled = $true
    if ($cs.PSObject.Properties.Name -contains "HypervisorPresent" -and $cs.HypervisorPresent) {
        $enabled = $true
    }
    else {
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        if ($cpu.PSObject.Properties.Name -contains "VirtualizationFirmwareEnabled") {
            $enabled = [bool]$cpu.VirtualizationFirmwareEnabled
        }
    }
    if ($enabled) {
        Add-Check -Name "Виртуализация" -State "ГОТОВО" -Detail "включена"
    }
    else {
        Add-Check -Name "Виртуализация" -State "ОСТАНОВКА" -Detail "выключена в BIOS/UEFI" `
            -Fix "Перезагрузите компьютер, войдите в BIOS/UEFI и включите виртуализацию (Intel VT-x или AMD-V)."
    }
}

# --- WSL -----------------------------------------------------------------------
Test-Safely "Подсистема Linux (WSL)" {
    $wsl = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wsl) {
        Add-Check -Name "Подсистема Linux (WSL)" -State "ВНИМАНИЕ" -Detail "не установлена" `
            -Fix "Установщик поставит её сам. Потребуется одна перезагрузка."
        return
    }
    # wsl.exe печатает список в UTF-16. Без смены кодировки консоли сюда
    # попадают не имена дистрибутивов, а мусор, и при ошибке WSL её текст
    # тоже приходит в этот же поток. Читаем правильно и смотрим код возврата.
    $previousEncoding = [Console]::OutputEncoding
    try {
        [Console]::OutputEncoding = [Text.Encoding]::Unicode
        $rawList = & wsl.exe -l -q 2>&1
        $wslExit = $LASTEXITCODE
    }
    finally { [Console]::OutputEncoding = $previousEncoding }
    if ($wslExit -ne 0) {
        $message = ($rawList | Out-String).Trim()
        Add-Check -Name "Подсистема Linux (WSL)" -State "ВНИМАНИЕ" `
            -Detail "WSL установлен, но отвечает ошибкой" `
            -Fix "WSL сообщает: $message. Установщик попробует подготовить подсистему заново; если ошибка повторится, обновите WSL командой wsl --update."
        return
    }
    $distros = @(
        $rawList | ForEach-Object { $_.Replace([string][char]0, "").Trim() } |
            Where-Object { $_ -and $_ -notmatch "^\s*$" }
    )
    if ($distros -contains $WslDistro) {
        Add-Check -Name "Подсистема Linux (WSL)" -State "ГОТОВО" -Detail "дистрибутив $WslDistro установлен"
    }
    elseif ($distros.Count -gt 0) {
        Add-Check -Name "Подсистема Linux (WSL)" -State "ВНИМАНИЕ" `
            -Detail "есть дистрибутивы: $($distros -join ', '), но нет $WslDistro" `
            -Fix "Установщик добавит $WslDistro. Существующие дистрибутивы не трогаются."
    }
    else {
        Add-Check -Name "Подсистема Linux (WSL)" -State "ВНИМАНИЕ" -Detail "дистрибутивов нет" `
            -Fix "Установщик поставит $WslDistro. Потребуется одна перезагрузка."
    }
}

# --- Docker Desktop ------------------------------------------------------------
Test-Safely "Docker Desktop" {
    $service = Get-Service -Name "com.docker.service" -ErrorAction SilentlyContinue
    if (-not $service) {
        Add-Check -Name "Docker Desktop" -State "ГОТОВО" -Detail "не установлен, он и не нужен"
    }
    elseif ($service.Status -eq "Running") {
        Add-Check -Name "Docker Desktop" -State "ВНИМАНИЕ" -Detail "установлен и запущен" `
            -Fix "Аварийная станция работает на Docker внутри WSL и не требует Docker Desktop. Конфликта обычно нет, но если Docker не поднимется, остановите Docker Desktop."
    }
    else {
        Add-Check -Name "Docker Desktop" -State "ГОТОВО" -Detail "установлен, но остановлен"
    }
}

# --- Память и диск -------------------------------------------------------------
Test-Safely "Оперативная память" {
    $gb = [math]::Round(((Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum).Sum) / 1GB, 1)
    if ($gb -ge 16) { Add-Check -Name "Оперативная память" -State "ГОТОВО" -Detail "$gb ГБ" }
    elseif ($gb -ge 8) {
        Add-Check -Name "Оперативная память" -State "ВНИМАНИЕ" -Detail "$gb ГБ" `
            -Fix "Минимум выполнен. Рекомендуется 16 ГБ, чтобы склад и база не конкурировали за память."
    }
    else {
        Add-Check -Name "Оперативная память" -State "ОСТАНОВКА" -Detail "$gb ГБ" `
            -Fix "Нужно не менее 8 ГБ оперативной памяти."
    }
}

Test-Safely "Свободное место" {
    $qualifier = try { Split-Path -Qualifier ([IO.Path]::GetFullPath($RepoRoot)) } catch { "C:" }
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$qualifier'" -ErrorAction SilentlyContinue
    if (-not $disk) {
        Add-Check -Name "Свободное место" -State "ВНИМАНИЕ" -Detail "диск $qualifier не найден" `
            -Fix "Укажите существующий диск через -RepoRoot."
        return
    }
    $free = [math]::Round($disk.FreeSpace / 1GB, 1)
    if ($free -ge 60) { Add-Check -Name "Свободное место" -State "ГОТОВО" -Detail "$free ГБ на $qualifier" }
    elseif ($free -ge 30) {
        Add-Check -Name "Свободное место" -State "ВНИМАНИЕ" -Detail "$free ГБ на $qualifier" `
            -Fix "Минимум выполнен. Рекомендуется 60 ГБ: образы, база, media и две копии standby растут со временем."
    }
    else {
        Add-Check -Name "Свободное место" -State "ОСТАНОВКА" -Detail "$free ГБ на $qualifier" `
            -Fix "Освободите место: нужно не менее 30 ГБ."
    }
}

# --- Сеть ----------------------------------------------------------------------
Test-Safely "Локальная сеть" {
    $all = @(
        Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
            Where-Object { $_.PrefixOrigin -ne "WellKnown" -and $_.IPAddress -notmatch '^(127\.|169\.254\.)' }
    )
    # Адреса WSL, Hyper-V, VPN и туннелей не принадлежат сети склада. Если
    # предложить такой адрес, ярлык на других компьютерах открываться не будет.
    $virtualPattern = "vEthernet|WSL|Hyper-V|VirtualBox|VMware|Loopback|TAP|TUN|tun|Tailscale|ZeroTier|happ"
    $addresses = @($all | Where-Object {
        $_.InterfaceAlias -notmatch $virtualPattern
    })
    $virtual = @($all | Where-Object { $_.InterfaceAlias -match $virtualPattern })
    if ($addresses.Count -eq 0) {
        $hint = if ($virtual.Count -gt 0) {
            "Найдены только виртуальные адаптеры: $(($virtual | ForEach-Object { $_.InterfaceAlias }) -join ', '). Это не сеть склада."
        } else { "" }
        Add-Check -Name "Локальная сеть" -State "ОСТАНОВКА" -Detail "нет адреса в сети склада" `
            -Fix "Подключите компьютер к сети склада кабелем или Wi-Fi. $hint"
        return
    }
    $list = ($addresses | ForEach-Object { "$($_.IPAddress) ($($_.InterfaceAlias))" }) -join "; "
    if ($virtual.Count -gt 0) {
        $list += "  [виртуальные пропущены: $($virtual.Count)]"
    }
    $dhcp = @($addresses | Where-Object { $_.PrefixOrigin -eq "Dhcp" })
    if ($dhcp.Count -eq $addresses.Count) {
        Add-Check -Name "Локальная сеть" -State "ВНИМАНИЕ" -Detail $list `
            -Fix "Адрес выдан автоматически и может смениться. Закрепите постоянный адрес за этим компьютером, иначе ярлык на других компьютерах перестанет открываться."
    }
    else {
        Add-Check -Name "Локальная сеть" -State "ГОТОВО" -Detail $list
    }
}

Test-Safely "Порт $Port" {
    $busy = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
    if (-not $busy) {
        Add-Check -Name "Порт $Port" -State "ГОТОВО" -Detail "свободен"
    }
    else {
        $names = @($busy | ForEach-Object {
            (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        } | Where-Object { $_ } | Select-Object -Unique) -join ", "
        Add-Check -Name "Порт $Port" -State "ОСТАНОВКА" -Detail "занят: $names" `
            -Fix "Освободите порт $Port или выберите другой при установке параметром -Port."
    }
}

# --- Уже установленное ---------------------------------------------------------
Test-Safely "Прежняя установка" {
    $runtimeRoot = Join-Path $RepoRoot ".emergency"
    $identity = Join-Path $runtimeRoot "workstation-id.txt"
    $envFile = Join-Path $RepoRoot ".env.emergency"
    if (-not (Test-Path -LiteralPath $runtimeRoot)) {
        Add-Check -Name "Прежняя установка" -State "ГОТОВО" -Detail "чистый компьютер"
        return
    }
    $parts = @()
    if (Test-Path -LiteralPath $identity) {
        $id = (Get-Content -LiteralPath $identity -Raw -ErrorAction SilentlyContinue).Trim()
        $parts += "идентификатор станции: $id"
    }
    if (Test-Path -LiteralPath $envFile) { $parts += "конфигурация есть" }
    Add-Check -Name "Прежняя установка" -State "ВНИМАНИЕ" -Detail ($parts -join "; ") `
        -Fix "Станция уже установлена. Повторный запуск установщика её не сломает: идентификатор и секреты сохранятся. Для обновления версии используйте обновление, а не переустановку."
}

# --- Часы ----------------------------------------------------------------------
Test-Safely "Часы компьютера" {
    $now = Get-Date
    $service = Get-Service -Name W32Time -ErrorAction SilentlyContinue
    $detail = $now.ToString("yyyy-MM-dd HH:mm:ss zzz")
    if ($service -and $service.Status -eq "Running") {
        Add-Check -Name "Часы компьютера" -State "ГОТОВО" -Detail "$detail, синхронизация включена"
    }
    else {
        Add-Check -Name "Часы компьютера" -State "ВНИМАНИЕ" -Detail $detail `
            -Fix "Служба времени Windows не запущена. Неверные часы мешают проверять свежесть копии склада."
    }
}

# --- Источник копий -------------------------------------------------------------
Test-Safely "Источник копий" {
    if (-not $BackupSource) {
        Add-Check -Name "Источник копий" -State "ВНИМАНИЕ" -Detail "не указан для проверки" `
            -Fix "Запустите проверку ещё раз с параметром -BackupSource yandex-s3:имя-хранилища. Без работающего источника станция встанет готовой, но забирать копии ей будет неоткуда."
        return
    }
    if (-not (Get-Command Test-EmergencyBackupSource -ErrorAction SilentlyContinue)) {
        Add-Check -Name "Источник копий" -State "ВНИМАНИЕ" -Detail "не найден EmergencyBackupSource.ps1"
        return
    }
    $probe = Test-EmergencyBackupSource -Source $BackupSource
    if ($probe.RcloneVersion) {
        Add-Check -Name "rclone" -State "ГОТОВО" -Detail $probe.RcloneVersion
    }
    elseif ($probe.Kind -eq "not-installed") {
        Add-Check -Name "rclone" -State "ОСТАНОВКА" -Detail "не установлен" `
            -Fix "Поставьте rclone и настройте источник копий, затем повторите проверку."
    }
    if ($probe.State -eq "ГОТОВО") {
        $age = Get-BackupRunAgeHours -RunId $probe.LatestRun
        $detail = "$($probe.Remote): $($probe.Detail)"
        if ($null -ne $age) { $detail += ", возраст $age ч" }
        Add-Check -Name "Источник копий" -State "ГОТОВО" -Detail $detail
    }
    else {
        Add-Check -Name "Источник копий" -State "ОСТАНОВКА" `
            -Detail "$($probe.Kind): $($probe.Detail)" -Fix $probe.Advice
    }
}

# --- Политика запуска сценариев -------------------------------------------------
Test-Safely "Запуск сценариев" {
    $policy = Get-ExecutionPolicy
    Add-Check -Name "Запуск сценариев" -State "ГОТОВО" `
        -Detail "текущая политика: $policy; установщик запускается с -ExecutionPolicy Bypass"
}

# --- Отчёт ----------------------------------------------------------------------
$width = ($script:Results | ForEach-Object { $_.Name.Length } | Measure-Object -Maximum).Maximum
foreach ($result in $script:Results) {
    $color = switch ($result.State) {
        "ГОТОВО" { "Green" }
        "ВНИМАНИЕ" { "Yellow" }
        default { "Red" }
    }
    Write-Host ("  {0}  " -f $result.Name.PadRight($width)) -NoNewline
    Write-Host $result.State.PadRight(10) -ForegroundColor $color -NoNewline
    Write-Host $result.Detail -ForegroundColor DarkGray
}

$blockers = @($script:Results | Where-Object { $_.State -eq "ОСТАНОВКА" })
$warnings = @($script:Results | Where-Object { $_.State -eq "ВНИМАНИЕ" })

if ($blockers.Count -gt 0 -or $warnings.Count -gt 0) {
    Write-Host ""
    Write-Host "Что нужно сделать:" -ForegroundColor Cyan
    foreach ($result in ($blockers + $warnings)) {
        if (-not $result.Fix) { continue }
        $mark = if ($result.State -eq "ОСТАНОВКА") { "[!]" } else { "[.]" }
        Write-Host "  $mark $($result.Name): $($result.Fix)"
    }
}

Write-Host ""
if ($blockers.Count -eq 0) {
    Write-Host "Можно устанавливать." -ForegroundColor Green
    if ($warnings.Count -gt 0) {
        Write-Host "Замечания выше установку не останавливают." -ForegroundColor DarkGray
    }
    exit 0
}

Write-Host "Устанавливать пока нельзя: $($blockers.Count) причин(ы) выше." -ForegroundColor Red
exit 1
