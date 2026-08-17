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
    [ValidateSet("primary", "secondary")]
    [string]$Role = "primary",
    [string]$WslDistro = "Ubuntu",
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
    $memoryGb = (Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB
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

Assert-Administrator
$RepoRoot = (Resolve-Path -LiteralPath $RepoRoot).Path
if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "rclone не найден, а winget недоступен. Установите rclone до provisioning."
    }
    & winget install --id Rclone.Rclone --exact --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) { throw "Не удалось установить rclone." }
    throw "rclone установлен. Откройте новый PowerShell, настройте approved remote и повторите provisioning."
}
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
if ((& git -C $RepoRoot status --porcelain --untracked-files=no).Count -gt 0) {
    throw "Provisioning запрещён из dirty checkout."
}
if ($Role -eq "primary") {
    if (-not $ConfirmPrimary) {
        throw "Для primary укажите -ConfirmPrimary после проверки, что это единственный LAN writer."
    }
    Assert-PrimaryNetwork $PrimaryLanAddress
    Assert-WorkstationCapacity
}

if ($InstallWslRuntime) {
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) { throw "WSL2 недоступен в Windows." }
    $installed = & wsl.exe -l -q 2>$null
    if ($LASTEXITCODE -ne 0 -or $installed -notcontains $WslDistro) {
        & wsl.exe --install -d $WslDistro
        throw "WSL distribution создана. Перезагрузите Windows, войдите в $WslDistro и повторите provisioning."
    }
    $bootstrap = ConvertTo-WslPath (Join-Path $RepoRoot "scripts\operations\provision-wsl-docker.sh")
    & wsl.exe -d $WslDistro -u root -- bash $bootstrap
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

$envFile = Join-Path $RepoRoot ".env.emergency"
if (Test-Path -LiteralPath $envFile) { throw ".env.emergency уже существует; provisioning не перезаписывает secrets." }
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
    "DENSTOCK_PRODUCTION_URL=$ProductionUrl",
    "DENSTOCK_EMERGENCY_PROBE_TOKEN=$probeValue",
    "AI_SUPPORT_ENABLED=false",
    "AI_SUPPORT_PROVIDER=disabled"
)
$envLines | Set-Content -LiteralPath $envFile -Encoding utf8NoBOM

$acl = Get-Acl -LiteralPath $envFile
$acl.SetAccessRuleProtection($true, $false)
foreach ($identity in @("BUILTIN\Administrators", [Security.Principal.WindowsIdentity]::GetCurrent().Name)) {
    $rule = New-Object Security.AccessControl.FileSystemAccessRule($identity, "FullControl", "Allow")
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $envFile -AclObject $acl

if ($Role -eq "primary") {
    $ruleName = "DenisStock Emergency LAN $Port"
    Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Profile Private | Out-Null
}

$launcher = Join-Path $RepoRoot "scripts\operations\DenisStock-Emergency.ps1"
$shortcutDir = [Environment]::GetFolderPath("CommonDesktopDirectory")
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
    $action = New-ScheduledTaskAction -Execute "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$refresh`""
    $triggers = @(
        (New-ScheduledTaskTrigger -Daily -At 7:00AM),
        (New-ScheduledTaskTrigger -Daily -At 7:00PM),
        (New-ScheduledTaskTrigger -AtLogOn)
    )
    Register-ScheduledTask -TaskName "DenisStock Emergency Standby Refresh" -Action $action -Trigger $triggers -Description "Refreshes verified DenisStock emergency standby only when no offline lifecycle exists." -Force | Out-Null
}

& powershell.exe -NoProfile -ExecutionPolicy Bypass -File $launcher -Action Status
if ($LASTEXITCODE -ne 0) { throw "Provisioning завершился, но emergency status self-test не прошёл." }
Write-Host "Emergency workstation provisioning completed. Run Sync before declaring standby READY." -ForegroundColor Green
