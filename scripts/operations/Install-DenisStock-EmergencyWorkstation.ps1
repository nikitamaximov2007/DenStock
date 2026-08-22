param(
    [Parameter(Mandatory)]
    [string]$RepoRoot,
    [Parameter(Mandatory)]
    [string]$BackupSource,
    [Parameter(Mandatory)]
    [string]$ProductionUrl,
    [Parameter(Mandatory)]
    [string]$PrimaryLanAddress,
    [Parameter(Mandatory)]
    [string]$AppCommit,
    [Parameter(Mandatory)]
    [string]$ReleaseSource,
    [Parameter(Mandatory)]
    [string]$ManifestPublicKeyPath,
    [Parameter(Mandatory)]
    [string]$ManifestSigningKeyId,
    # Отпечаток закрепляемого ключа. Проверяется всегда: подменённый публичный
    # ключ обязан остановить установку, а не тихо стать доверенным.
    [string]$ExpectedPublicKeyFingerprint = "5615837ef355d2d1881508434980efac31f1c467acb3d31c57101ced3ee5d5b1",
    [ValidateSet("primary", "secondary")]
    [string]$Role = "primary",
    [string]$WslDistro = "Ubuntu",
    [ValidateRange(1, 65535)]
    [int]$Port = 8080,
    [switch]$InstallWslRuntime,
    [switch]$CreateTasks,
    [switch]$ConfirmPrimary
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Assert-Administrator {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw "Provisioning запускается только из PowerShell от имени администратора."
    }
}

function New-RandomSecret {
    $bytes = New-Object byte[] 36
    [Security.Cryptography.RandomNumberGenerator]::Fill($bytes)
    return [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
}

function Assert-PrimaryNetwork {
    param([string]$Address)
    $match = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -eq $Address -and $_.PrefixOrigin -ne "WellKnown" }
    if (-not $match) {
        throw "Primary LAN address $Address не принадлежит этому компьютеру. Используйте текущий LAN IPv4."
    }
    if ($Address -match '^(127\.|169\.254\.)') {
        throw "Loopback и link-local адрес нельзя использовать для LAN emergency server."
    }
}

function Assert-WorkstationCapacity {
    $memoryGb = ((Get-CimInstance Win32_PhysicalMemory | Measure-Object -Property Capacity -Sum).Sum) / 1GB
    $root = Split-Path -Qualifier (Resolve-Path -LiteralPath $RepoRoot).Path
    $disk = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$root'"
    if ($memoryGb -lt 8) { throw "Для Emergency Primary требуется не менее 8 GB RAM." }
    if (($disk.FreeSpace / 1GB) -lt 30) { throw "Для Emergency Primary требуется не менее 30 GB свободного диска." }
}

function ConvertTo-WslPath {
    param([string]$Path)
    $full = [IO.Path]::GetFullPath($Path)
    if ($full -notmatch '^([A-Za-z]):\\(.*)$') { throw "Не Windows path: $full" }
    "/mnt/$($Matches[1].ToLower())/$($Matches[2].Replace('\', '/'))"
}

function Assert-SingleLineConfig {
    param([string]$Name, [string]$Value)
    if (-not $Value -or $Value -match "[`r`n]") {
        throw "$Name отсутствует или содержит недопустимый перенос строки."
    }
}

function Get-PublicKeyFingerprint {
    <#
        SHA-256 от DER-представления публичного ключа. Та же величина, что даёт
        на сервере "openssl pkey -pubin -outform DER | openssl dgst -sha256",
        поэтому администратор может сверить её с production, не передавая файл.
    #>
    param([string]$Path)
    $pem = Get-Content -LiteralPath $Path -Raw
    $base64 = ($pem -replace "-----BEGIN PUBLIC KEY-----", "" `
                    -replace "-----END PUBLIC KEY-----", "" `
                    -replace "\s", "")
    if (-not $base64) { throw "Файл публичного ключа пуст или не в формате PEM: $Path" }
    try { $der = [Convert]::FromBase64String($base64) }
    catch { throw "Файл публичного ключа не читается как PEM: $Path" }
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($der)) -replace "-", "").ToLower() }
    finally { $sha.Dispose() }
}

function Set-AdministratorOnlyAcl {
    param([string]$Path, [switch]$Directory)
    $acl = Get-Acl -LiteralPath $Path
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($identity in @("BUILTIN\Administrators", [Security.Principal.WindowsIdentity]::GetCurrent().Name)) {
        if ($Directory) {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule(
                $identity, "FullControl", "ContainerInherit, ObjectInherit", "None", "Allow"
            )
        }
        else {
            $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")
        }
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $Path -AclObject $acl
}

Assert-Administrator
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
foreach ($required in @("docker-compose.emergency.yml", ".env.emergency.example", "scripts\operations\DenisStock-Emergency.ps1")) {
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot $required))) {
        throw "Не найден DenisStock Emergency release: $required"
    }
}
if ($AppCommit -notmatch '^[0-9a-f]{40}$') { throw "AppCommit должен быть полным SHA-1 release." }
if (-not (Get-Command git -ErrorAction SilentlyContinue)) { throw "Git требуется только для проверки release identity." }
$head = (& git -C $RepoRoot rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $head -cne $AppCommit) {
    throw "Checkout не совпадает с указанным production release commit."
}
if (@(& git -C $RepoRoot status --porcelain --untracked-files=no).Count -gt 0) {
    throw "Provisioning запрещён из dirty checkout."
}
if ($Role -eq "primary") {
    if (-not $ConfirmPrimary) {
        throw "Для primary укажите -ConfirmPrimary после проверки, что это единственный LAN writer."
    }
    Assert-PrimaryNetwork $PrimaryLanAddress
    Assert-WorkstationCapacity
}
foreach ($item in @(
    @{ Name = "BackupSource"; Value = $BackupSource },
    @{ Name = "ReleaseSource"; Value = $ReleaseSource },
    @{ Name = "ProductionUrl"; Value = $ProductionUrl },
    @{ Name = "PrimaryLanAddress"; Value = $PrimaryLanAddress }
)) {
    Assert-SingleLineConfig -Name $item.Name -Value $item.Value
}
if ($ManifestSigningKeyId -notmatch '^[A-Za-z0-9._-]{1,128}$') {
    throw "ManifestSigningKeyId содержит недопустимые символы."
}
if (-not (Test-Path -LiteralPath $ManifestPublicKeyPath -PathType Leaf)) {
    throw "Pinned production manifest public key не найден."
}
if ($ExpectedPublicKeyFingerprint -notmatch "^[0-9a-f]{64}$") {
    throw "ExpectedPublicKeyFingerprint должен быть 64 шестнадцатеричными знаками SHA-256."
}
$sourceFingerprint = Get-PublicKeyFingerprint -Path $ManifestPublicKeyPath
if ($sourceFingerprint -ne $ExpectedPublicKeyFingerprint.ToLower()) {
    throw @"
Отпечаток публичного ключа не совпадает с ожидаемым. Установка остановлена.
  ожидался: $ExpectedPublicKeyFingerprint
  получен:  $sourceFingerprint
Это либо не тот файл, либо ключ подменили. Возьмите ключ у администратора
production заново и сверьте отпечаток, прежде чем повторять установку.
"@
}
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "rclone не найден, а winget недоступен. Установите rclone до provisioning."
    }
    & winget install --id Rclone.Rclone --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить rclone." }
    throw "rclone установлен. Откройте новый PowerShell, настройте approved remote и повторите provisioning."
}

if ($InstallWslRuntime) {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw "WSL2 недоступен в Windows." }
    $installed = & wsl.exe -l -q 2>$null
    if ($LASTEXITCODE -ne 0 -or $installed -notcontains $WslDistro) {
        & wsl.exe --install -d $WslDistro
        throw "WSL distribution создана. Перезагрузите Windows, войдите в $WslDistro и повторите provisioning."
    }
    $bootstrapPath = Join-Path $RepoRoot "scripts\operations\provision-wsl-docker.sh"
    # Windows-копия могла приехать с возвратом каретки. Linux такой файл не
    # запустит, и bash сообщит про «invalid option name» - причину по этому
    # сообщению не найти. Поэтому останавливаемся здесь и говорим прямо.
    if (@([IO.File]::ReadAllBytes($bootstrapPath)) -contains 13) {
        throw "Сценарий подготовки WSL записан в формате Windows и внутри Linux не запустится. Выполните: powershell -ExecutionPolicy Bypass -File $RepoRoot\scripts\operations\Repair-DenisStockShellLineEndings.ps1 -RepoRoot $RepoRoot"
    }
    $bootstrap = ConvertTo-WslPath $bootstrapPath
    & wsl.exe -d $WslDistro -u root -- bash $bootstrap
    if ($LASTEXITCODE -eq 42) {
        throw "WSL systemd включён. Выполните 'wsl --shutdown' из Administrator PowerShell и повторите provisioning."
    }
    if ($LASTEXITCODE -ne 0) { throw "Не удалось подготовить WSL Docker Engine." }
    throw "WSL Docker Engine установлен. Закройте WSL session и повторите provisioning без -InstallWslRuntime."
}

& wsl.exe -d $WslDistro -- docker info *> $null
if ($LASTEXITCODE -ne 0) {
    throw "WSL2 Docker Engine не готов. Повторите с -InstallWslRuntime или устраните Docker Engine в $WslDistro."
}

$probe = Read-Host "Введите shared production probe token (не будет показан)" -AsSecureString
$probePtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($probe)
try { $probeValue = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($probePtr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($probePtr) }
if (-not $probeValue) { throw "Probe token обязателен для безопасного failback check." }

$runtimeRoot = Join-Path $RepoRoot ".emergency"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
Set-AdministratorOnlyAcl -Path $runtimeRoot -Directory
$identityFile = Join-Path $runtimeRoot "workstation-id.txt"
if (Test-Path -LiteralPath $identityFile) {
    try { $workstationId = [Guid]::Parse((Get-Content -LiteralPath $identityFile -Raw -Encoding UTF8).Trim()).ToString() }
    catch { throw "Существующий workstation UUID повреждён. Не создавайте новый UUID поверх него." }
}
else {
    $workstationId = [Guid]::NewGuid().ToString()
    Set-Content -LiteralPath $identityFile -Value $workstationId -Encoding utf8NoBOM -NoNewline
    Set-AdministratorOnlyAcl -Path $identityFile
}
$trustedKeyDir = Join-Path $runtimeRoot "trusted"
New-Item -ItemType Directory -Force -Path $trustedKeyDir | Out-Null
Set-AdministratorOnlyAcl -Path $trustedKeyDir -Directory
$pinnedPublicKey = Join-Path $trustedKeyDir "production-manifest-ed25519-public.pem"
if (Test-Path -LiteralPath $pinnedPublicKey) {
    # Повторный запуск не заменяет доверенный ключ, но обязан убедиться, что
    # закреплён именно ожидаемый. Совпал - продолжаем, разошёлся - остановка.
    $pinnedFingerprint = Get-PublicKeyFingerprint -Path $pinnedPublicKey
    if ($pinnedFingerprint -ne $ExpectedPublicKeyFingerprint.ToLower()) {
        throw @"
На станции закреплён другой публичный ключ. Установка остановлена.
  закреплён: $pinnedFingerprint
  ожидается: $ExpectedPublicKeyFingerprint
Замена доверенного ключа - отдельная процедура ротации, а не переустановка.
"@
    }
    Write-Host "Публичный ключ уже закреплён, отпечаток совпадает." -ForegroundColor DarkGray
}
else {
    Copy-Item -LiteralPath $ManifestPublicKeyPath -Destination $pinnedPublicKey
    Set-AdministratorOnlyAcl -Path $pinnedPublicKey
    Write-Host "Публичный ключ закреплён: $sourceFingerprint" -ForegroundColor DarkGray
}

# Путь к настройкам rclone определяется здесь, при установке, и записывается в
# конфигурацию станции. Причина в задании обновления копии: оно идёт с типом
# входа S4U, и полагаться на то, что Windows подставит профиль и переменную
# APPDATA, нельзя. Обёртка обновления читает этот путь и задаёт RCLONE_CONFIG
# явно, поэтому источник копий находится независимо от профиля.
#
# Это путь, а не секрет. Сам файл не читается, не копируется и в репозиторий
# не попадает.
$rcloneConfigPath = if ($env:RCLONE_CONFIG) { $env:RCLONE_CONFIG }
                    else { Join-Path $env:APPDATA "rclone" | Join-Path -ChildPath "rclone.conf" }
if (-not (Test-Path -LiteralPath $rcloneConfigPath -PathType Leaf)) {
    throw @"
Не найдены настройки rclone: $rcloneConfigPath
Выполните rclone config под этой же учётной записью Windows и создайте источник
копий только на чтение, затем повторите установку. Задание обновления копии
будет работать от этой учётной записи и читать именно этот файл.
"@
}

$envFile = Join-Path $RepoRoot ".env.emergency"
$envExisted = Test-Path -LiteralPath $envFile
if ($envExisted) {
    # Повторный запуск не трогает уже сгенерированные секреты станции: пароль
    # базы и ключ Django остаются прежними, иначе установка поверх рабочей
    # станции разорвала бы ей доступ к собственной базе.
    Write-Host "Конфигурация станции уже создана, секреты сохранены без изменений." -ForegroundColor DarkGray
}
else {
$bindHost = if ($Role -eq "primary") { $PrimaryLanAddress } else { "127.0.0.1" }
$postgresPassword = New-RandomSecret
$allowedHosts = "localhost,127.0.0.1,emergency-web" + $(if ($Role -eq "primary") { ",$PrimaryLanAddress" } else { "" })
$trustedOrigins = "http://localhost:$Port,http://127.0.0.1:$Port" + $(if ($Role -eq "primary") { ",http://$PrimaryLanAddress`:$Port" } else { "" })
$envLines = @(
    "DJANGO_SETTINGS_MODULE=config.settings.prod",
    "DJANGO_SECRET_KEY=$(New-RandomSecret)",
    "DJANGO_DEBUG=false",
    "DJANGO_ALLOWED_HOSTS=$allowedHosts",
    "DJANGO_CSRF_TRUSTED_ORIGINS=$trustedOrigins",
    "DJANGO_SECURE_COOKIES=false",
    "TIME_ZONE=Europe/Moscow",
    "POSTGRES_DB=denstock_emergency_control",
    "POSTGRES_USER=denstock_emergency",
    "POSTGRES_PASSWORD=$postgresPassword",
    "DATABASE_URL=postgresql://denstock_emergency:$postgresPassword@emergency-db:5432/denstock_emergency_control",
    "DENSTOCK_MODE=emergency-local",
    "DENSTOCK_INSTANCE_ID=$env:COMPUTERNAME",
    "DENSTOCK_EMERGENCY_WORKSTATION_ID=$workstationId",
    "DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH=/app/.emergency/workstation-id.txt",
    "DENSTOCK_MANIFEST_PUBLIC_KEY_PATH=/app/.emergency/trusted/production-manifest-ed25519-public.pem",
    "DENSTOCK_MANIFEST_SIGNING_KEY_ID=$ManifestSigningKeyId",
    "DENSTOCK_APP_COMMIT=$AppCommit",
    "DENSTOCK_EMERGENCY_DB_PREFIX=denstock_emergency_",
    "DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS=emergency-db,localhost,127.0.0.1",
    "DENSTOCK_PRODUCTION_DB_HOSTS=185.250.44.206,db",
    "DENSTOCK_EMERGENCY_ROLE=$Role",
    "DENSTOCK_EMERGENCY_RUNTIME=wsl2",
    "DENSTOCK_EMERGENCY_WSL_DISTRO=$WslDistro",
    "DENSTOCK_EMERGENCY_BIND_HOST=$bindHost",
    "DENSTOCK_EMERGENCY_PORT=$Port",
    "DENSTOCK_EMERGENCY_STALE_WARNING_HOURS=24",
    "DENSTOCK_EMERGENCY_KEEP_STANDBY=2",
    "DENSTOCK_EMERGENCY_KEEP_DOWNLOADS=2",
    "DENSTOCK_EMERGENCY_KEEP_COMPLETED_EXPORTS=2",
    "DENSTOCK_EMERGENCY_BACKUP_SOURCE=$BackupSource",
    "DENSTOCK_EMERGENCY_RCLONE_CONFIG=$rcloneConfigPath",
    "DENSTOCK_EMERGENCY_RELEASE_SOURCE=$ReleaseSource",
    "DENSTOCK_PRODUCTION_URL=$ProductionUrl",
    "DENSTOCK_EMERGENCY_PROBE_TOKEN=$probeValue",
    "AI_SUPPORT_ENABLED=false",
    "AI_SUPPORT_PROVIDER=disabled"
)
$envLines | Set-Content -LiteralPath $envFile -Encoding utf8NoBOM
Set-AdministratorOnlyAcl -Path $envFile
Write-Host "Конфигурация станции создана." -ForegroundColor DarkGray
}

if ($Role -eq "primary") {
    $ruleName = "DenisStock Emergency LAN $Port"
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private | Out-Null
}

$launcher = Join-Path $RepoRoot "scripts\operations\DenisStock-Emergency.ps1"
# The launcher can read emergency secrets, so it belongs only to the responsible
# administrator's desktop. LAN clients receive a browser-only shortcut separately.
$shortcutDir = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
foreach ($item in @(
    @{ Name = "DenisStock - Аварийный.lnk"; Args = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`"" },
    @{ Name = "DenisStock - Аварийный статус.lnk"; Args = "-NoProfile -ExecutionPolicy Bypass -File `"$launcher`" -Action Status" }
)) {
    $shortcut = $shell.CreateShortcut((Join-Path $shortcutDir $item.Name))
    $shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
    $shortcut.Arguments = $item.Args
    $shortcut.WorkingDirectory = $RepoRoot
    $shortcut.Save()
}

if ($CreateTasks) {
    $refresh = Join-Path $RepoRoot "scripts\operations\Emergency-Standby-Refresh.ps1"
    $action = New-ScheduledTaskAction `
        -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$refresh`"" `
        -WorkingDirectory $RepoRoot
    $triggers = @(
        (New-ScheduledTaskTrigger -Daily -At 7:00AM),
        (New-ScheduledTaskTrigger -Daily -At 7:00PM),
        (New-ScheduledTaskTrigger -AtLogOn)
    )
    # Учётная запись задания задаётся явно и намеренно.
    #
    # Обновление копии выполняет rclone, а его настройки лежат в профиле
    # пользователя: %APPDATA%\rclone\rclone.conf. Если задание пойдёт под
    # СИСТЕМОЙ или под другим пользователем, у него будет другой профиль,
    # источник копий просто не найдётся, и станция начнёт молча устаревать.
    # Поэтому задание закрепляется за тем же, кто настраивал rclone и ставил
    # станцию.
    #
    # Тип входа S4U, а не Interactive: при Interactive ежедневные запуски в
    # 07:00 и 19:00 срабатывают только тогда, когда этот пользователь в
    # системе. Компьютер склада может стоять с заблокированным экраном, и
    # копия не обновлялась бы неделями. S4U работает без входа и не требует
    # хранить пароль.
    #
    # Права обычные, не повышенные: обновлению копии хватает доступа к своим
    # каталогам, которые установщик открыл этому пользователю.
    $taskAccount = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $principal = New-ScheduledTaskPrincipal -UserId $taskAccount -LogonType S4U -RunLevel Limited
    # Пропущенный запуск догоняется: компьютер склада ночью может быть выключен.
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Register-ScheduledTask -TaskName "DenisStock Emergency Standby Refresh" `
        -Action $action -Trigger $triggers -Principal $principal -Settings $settings `
        -Description "Обновляет проверенную аварийную копию склада, пока не начат автономный режим." `
        -Force | Out-Null
    Write-Host "Задание обновления копии закреплено за $taskAccount (вход S4U)." -ForegroundColor DarkGray
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher -Action Status
if ($LASTEXITCODE -ne 0) { throw "Provisioning завершился, но emergency status self-test не прошёл." }
Write-Host "Emergency workstation provisioning completed. Run Sync before declaring standby READY." -ForegroundColor Green
