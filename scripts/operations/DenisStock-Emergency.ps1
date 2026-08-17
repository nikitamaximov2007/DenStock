param(
    [ValidateSet("Menu", "Status", "Sync", "Start", "Stop", "FailbackCheck", "Package", "Complete", "Prune", "Open")]
    [string]$Action = "Menu",
    [switch]$NonInteractive,
    [ValidateSet("auto", "docker-desktop", "wsl2")]
    [string]$Runtime = "auto"
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$script:RepoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$script:EnvFile = Join-Path $script:RepoRoot ".env.emergency"
$script:ComposeFile = Join-Path $script:RepoRoot "docker-compose.emergency.yml"
$script:RuntimeRoot = Join-Path $script:RepoRoot ".emergency"
$script:ControlFile = Join-Path $script:RuntimeRoot "control.json"
$script:RefreshStatusFile = Join-Path $script:RuntimeRoot "standby-refresh-status.json"
$script:DownloadsRoot = Join-Path $script:RuntimeRoot "downloads"
$script:LogRoot = Join-Path $script:RuntimeRoot "logs"
New-Item -ItemType Directory -Force -Path $script:RuntimeRoot | Out-Null
try {
    $script:OperatorLock = [System.IO.File]::Open(
        (Join-Path $script:RuntimeRoot "operator.lock"),
        [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite,
        [System.IO.FileShare]::None
    )
}
catch {
    throw "Другая операция DenisStock Emergency Control уже выполняется."
}
$script:RuntimeKind = $null
$script:WslDistro = $null

Set-Location $script:RepoRoot

function Write-OperatorLog {
    param([string]$Event, [string]$Outcome, [string]$Details = "")
    New-Item -ItemType Directory -Force -Path $script:LogRoot | Out-Null
    $line = "{0}`t{1}`t{2}`t{3}" -f (
        [DateTimeOffset]::Now.ToString("o"), $Event, $Outcome, $Details
    )
    Add-Content -LiteralPath (Join-Path $script:LogRoot "emergency-control.log") -Value $line
}

function Read-DotEnv {
    if (-not (Test-Path -LiteralPath $script:EnvFile -PathType Leaf)) {
        throw "Файл .env.emergency не найден. Создайте его из .env.emergency.example."
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $script:EnvFile -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#") -or -not $trimmed.Contains("=")) {
            continue
        }
        $pair = $trimmed.Split("=", 2)
        $values[$pair[0].Trim()] = $pair[1].Trim()
    }
    return $values
}

function Set-DotEnvValue {
    param([string]$Key, [string]$Value)
    if ($Key -notmatch '^[A-Z][A-Z0-9_]*$' -or $Value -match "[`r`n]") {
        throw "Небезопасное значение emergency configuration."
    }
    $lines = @(Get-Content -LiteralPath $script:EnvFile -Encoding UTF8)
    $found = $false
    $updated = foreach ($line in $lines) {
        if ($line -match "^$([regex]::Escape($Key))=") {
            $found = $true
            "$Key=$Value"
        }
        else {
            $line
        }
    }
    if (-not $found) { $updated += "$Key=$Value" }
    $temporary = "$script:EnvFile.tmp-$([Guid]::NewGuid().ToString('N'))"
    try {
        $updated | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
        Move-Item -LiteralPath $temporary -Destination $script:EnvFile -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Write-StandbyRefreshStatus {
    param(
        [ValidateSet("ready", "failed", "incompatible")]
        [string]$Outcome,
        [string]$RunId = "",
        [string]$AppCommit = "",
        [string]$Code = ""
    )
    $payload = [ordered]@{
        schema_version = 1
        attempted_at = [DateTimeOffset]::Now.ToString("o")
        outcome = $Outcome
        backup_run_id = $RunId
        observed_app_commit = $AppCommit
        code = $Code
    }
    $temporary = "$script:RefreshStatusFile.tmp-$([Guid]::NewGuid().ToString('N'))"
    try {
        $payload | ConvertTo-Json | Set-Content -LiteralPath $temporary -Encoding utf8NoBOM
        Move-Item -LiteralPath $temporary -Destination $script:RefreshStatusFile -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Show-StandbyRefreshWarning {
    if (-not (Test-Path -LiteralPath $script:RefreshStatusFile -PathType Leaf)) { return }
    try {
        $status = Get-Content -LiteralPath $script:RefreshStatusFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        Write-Host "НЕ ГОТОВО: состояние последней standby sync повреждено." -ForegroundColor Red
        return
    }
    if ([string]$status.outcome -eq "ready") { return }
    $commit = [string]$status.observed_app_commit
    $suffix = if ($commit) { " Production backup commit: $commit." } else { "" }
    Write-Host "НЕ ГОТОВО: последняя standby sync не завершилась ($($status.outcome)).$suffix" -ForegroundColor Red
}

function Convert-ToWslPath {
    param([string]$Path)
    $full = [System.IO.Path]::GetFullPath($Path)
    if ($full -notmatch "^([A-Za-z]):\\(.*)$") {
        throw "Не удалось преобразовать путь для WSL: $full"
    }
    return "/mnt/$($Matches[1].ToLower())/$($Matches[2].Replace('\', '/'))"
}

function Initialize-EmergencyRuntime {
    param([hashtable]$Environment)
    $requested = if ($Runtime -ne "auto") { $Runtime } else { [string]$Environment["DENSTOCK_EMERGENCY_RUNTIME"] }
    if (-not $requested) { $requested = "wsl2" }
    if ($requested -eq "auto") { $requested = "wsl2" }

    if ($requested -eq "docker-desktop") {
        $script:RuntimeKind = "docker-desktop"
        return
    }
    if ($requested -ne "wsl2") {
        throw "Неподдерживаемый emergency runtime: $requested"
    }
    if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
        throw "WSL2 не установлен. Запустите workstation provisioning от администратора."
    }
    $distro = [string]$Environment["DENSTOCK_EMERGENCY_WSL_DISTRO"]
    if (-not $distro) { $distro = "Ubuntu" }
    & wsl.exe -d $distro -- docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "WSL2 Docker Engine недоступен в $distro. Docker Desktop для emergency runtime не требуется."
    }
    $script:RuntimeKind = "wsl2"
    $script:WslDistro = $distro
}

function Assert-Prerequisites {
    foreach ($command in @("git")) {
        if (-not (Get-Command $command -ErrorAction SilentlyContinue)) {
            throw "Не найдена команда $command."
        }
    }
    if ($script:RuntimeKind -eq "docker-desktop") {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
            throw "Docker CLI не найден."
        }
        & docker info *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Docker Desktop runtime недоступен."
        }
    }
}

function Assert-ReleaseIdentity {
    param([hashtable]$Environment)
    $configured = $Environment["DENSTOCK_APP_COMMIT"]
    if (-not $configured -or $configured.StartsWith("REPLACE-")) {
        throw "DENSTOCK_APP_COMMIT не настроен в .env.emergency."
    }
    $head = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0 -or $head -ne $configured) {
        throw "Текущий git HEAD не совпадает с DENSTOCK_APP_COMMIT."
    }
    $dirty = @(& git status --porcelain --untracked-files=no)
    if ($LASTEXITCODE -ne 0 -or $dirty.Count -gt 0) {
        throw "Рабочее дерево содержит отслеживаемые изменения. Сборка заблокирована."
    }
    $untracked = @(& git ls-files --others --exclude-standard)
    $unexpected = @($untracked | Where-Object {
        $_ -notmatch '^tools/research/\.(cache|runtime)/'
    })
    if ($LASTEXITCODE -ne 0 -or $unexpected.Count -gt 0) {
        throw "Рабочее дерево содержит посторонние untracked-файлы. Сборка заблокирована."
    }
}

function Get-BackupManifest {
    param([string]$RunPath)
    $manifestPath = Join-Path $RunPath "manifest.json"
    try {
        $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Скачанный backup manifest повреждён."
    }
    $commit = [string]$manifest.app_commit
    if ($commit -notmatch '^[0-9a-f]{40}$') {
        throw "Backup manifest не содержит полный безопасный app_commit."
    }
    return $manifest
}

function Set-ReleaseForBackup {
    param([hashtable]$Environment, [object]$Manifest)
    $target = [string]$Manifest.app_commit
    $current = (& git rev-parse HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Не удалось определить текущий application commit." }
    if ($current -eq $target -and [string]$Environment["DENSTOCK_APP_COMMIT"] -eq $target) {
        return $null
    }
    $source = [string]$Environment["DENSTOCK_EMERGENCY_RELEASE_SOURCE"]
    if (-not $source -or $source.StartsWith("REPLACE-")) {
        throw "Новый backup требует другой app_commit, но release source не настроен. Старый READY standby сохранён."
    }
    if (@(& git status --porcelain --untracked-files=no).Count -gt 0) {
        throw "Нельзя менять application release из dirty checkout."
    }
    $previousAppCommit = [string]$Environment["DENSTOCK_APP_COMMIT"]
    & git fetch --no-tags $source $target
    if ($LASTEXITCODE -ne 0) { throw "Не удалось получить exact application commit для backup." }
    $resolved = (& git rev-parse "$target^{commit}").Trim()
    if ($LASTEXITCODE -ne 0 -or $resolved -ne $target) {
        throw "Release source не предоставил exact application commit из manifest."
    }
    $checkoutChanged = $false
    try {
        & git checkout --detach $target
        if ($LASTEXITCODE -ne 0) { throw "Не удалось переключить application checkout на backup commit." }
        $checkoutChanged = $true
        Set-DotEnvValue -Key "DENSTOCK_APP_COMMIT" -Value $target
        $Environment["DENSTOCK_APP_COMMIT"] = $target
    }
    catch {
        if ($checkoutChanged) {
            & git checkout --detach $current
            if ($LASTEXITCODE -eq 0) {
                Set-DotEnvValue -Key "DENSTOCK_APP_COMMIT" -Value $previousAppCommit
                $Environment["DENSTOCK_APP_COMMIT"] = $previousAppCommit
            }
        }
        throw
    }
    return @{ Head = $current; AppCommit = $previousAppCommit }
}

function Restore-PreviousRelease {
    param([hashtable]$Environment, [object]$Previous)
    if ($null -eq $Previous) { return }
    & git checkout --detach $Previous.Head
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось вернуть прежний application checkout после failed standby refresh."
    }
    Set-DotEnvValue -Key "DENSTOCK_APP_COMMIT" -Value $Previous.AppCommit
    $Environment["DENSTOCK_APP_COMMIT"] = $Previous.AppCommit
}

function Invoke-EmergencyCompose {
    param([string[]]$CommandArgs, [switch]$AllowFailure)
    $composeArgs = @(
        "compose", "--project-name", "denstock-emergency",
        "--env-file", $script:EnvFile, "-f", $script:ComposeFile
    ) + $CommandArgs
    if ($script:RuntimeKind -eq "wsl2") {
        $linuxRoot = Convert-ToWslPath $script:RepoRoot
        $linuxEnv = Convert-ToWslPath $script:EnvFile
        $linuxCompose = Convert-ToWslPath $script:ComposeFile
        $composeArgs = @(
            "compose", "--project-name", "denstock-emergency",
            "--env-file", $linuxEnv, "-f", $linuxCompose
        ) + $CommandArgs
        $runtimeEnv = @(
            "EMERGENCY_DATABASE_NAME=$env:EMERGENCY_DATABASE_NAME",
            "EMERGENCY_SLOT=$env:EMERGENCY_SLOT"
        )
        & wsl.exe -d $script:WslDistro --cd $linuxRoot -- env @runtimeEnv docker @composeArgs | Out-Host
    }
    else {
        & docker @composeArgs | Out-Host
    }
    $exitCode = $LASTEXITCODE
    if ($exitCode -ne 0 -and -not $AllowFailure) {
        throw "Docker Compose завершился с кодом $exitCode."
    }
    return $exitCode
}

function Get-ControlState {
    if (-not (Test-Path -LiteralPath $script:ControlFile -PathType Leaf)) {
        return $null
    }
    try {
        return Get-Content -LiteralPath $script:ControlFile -Raw -Encoding UTF8 | ConvertFrom-Json
    }
    catch {
        throw "Файл .emergency/control.json повреждён."
    }
}

function Set-TargetFromControl {
    $control = Get-ControlState
    if ($null -ne $control -and $null -ne $control.active_standby) {
        $databaseName = [string]$control.active_standby.database_name
        $slot = [string]$control.active_standby.slot
        $prefix = [string]$script:Environment["DENSTOCK_EMERGENCY_DB_PREFIX"]
        $expectedCommit = [string]$script:Environment["DENSTOCK_APP_COMMIT"]
        if (-not $prefix) {
            $prefix = "denstock_emergency_"
        }
        if (
            $slot -notmatch '^[0-9a-f]{12}$' -or
            $databaseName -notmatch '^[A-Za-z_][A-Za-z0-9_]{0,62}$' -or
            $databaseName -cne "$prefix$slot" -or
            [string]$control.active_standby.app_commit -cne $expectedCommit
        ) {
            throw "Active standby не соответствует безопасному target или release commit."
        }
        $env:EMERGENCY_DATABASE_NAME = $databaseName
        $env:EMERGENCY_SLOT = $slot
    }
    else {
        $env:EMERGENCY_DATABASE_NAME = "denstock_emergency_control"
        $env:EMERGENCY_SLOT = "inactive"
    }
    return $control
}

function Set-ControlTarget {
    $prefix = [string]$script:Environment["DENSTOCK_EMERGENCY_DB_PREFIX"]
    if (-not $prefix) {
        $prefix = "denstock_emergency_"
    }
    $env:EMERGENCY_DATABASE_NAME = "${prefix}control"
    $env:EMERGENCY_SLOT = "inactive"
}

function Start-Database {
    Invoke-EmergencyCompose -CommandArgs @("up", "-d", "emergency-db") | Out-Null
}

function Invoke-Manage {
    param([string[]]$ManageArgs, [switch]$AllowFailure)
    $arguments = @("run", "--rm", "emergency-web", "python", "manage.py") + $ManageArgs
    return Invoke-EmergencyCompose -CommandArgs $arguments -AllowFailure:$AllowFailure
}

function Confirm-Exact {
    param([string]$Phrase, [string]$Prompt)
    if ($NonInteractive) {
        throw "Опасное действие запрещено в non-interactive режиме."
    }
    Write-Host $Prompt -ForegroundColor Yellow
    $answer = Read-Host "Введите точно: $Phrase"
    if ($answer -cne $Phrase) {
        throw "Подтверждение не совпало. Действие отменено."
    }
}

function Remove-DirectorySafely {
    param([string]$Path, [string]$AllowedRoot)
    $target = [System.IO.Path]::GetFullPath($Path)
    $root = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd("\", "/")
    if (-not $target.StartsWith($root + [System.IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Отказ удаления пути вне разрешённого runtime root: $target"
    }
    if (Test-Path -LiteralPath $target) {
        Remove-Item -LiteralPath $target -Recurse -Force
    }
}

function Get-LatestBackup {
    param([string]$Source)
    $isWindowsPath = $Source -match "^[A-Za-z]:[\\/]"
    $isRemote = (-not $isWindowsPath) -and $Source -match "^[A-Za-z0-9_.-]+:"
    if ($isRemote) {
        if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
            throw "Для offsite sync установите rclone и настройте remote в профиле пользователя."
        }
        $runs = @(& rclone lsf $Source --dirs-only)
        if ($LASTEXITCODE -ne 0) {
            throw "rclone не смог получить список backup runs."
        }
        $runId = $runs | ForEach-Object { $_.Trim().TrimEnd("/") } |
            Where-Object { $_ -match "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" } |
            Sort-Object -Descending | Select-Object -First 1
        if (-not $runId) {
            throw "В remote нет подходящего backup run."
        }
        return @{ RunId = $runId; Remote = $true; SourcePath = "$($Source.TrimEnd('/'))/$runId" }
    }

    $resolved = (Resolve-Path -LiteralPath $Source).Path
    if (Test-Path -LiteralPath (Join-Path $resolved "manifest.json") -PathType Leaf) {
        $run = Split-Path -Leaf $resolved
        return @{ RunId = $run; Remote = $false; SourcePath = $resolved }
    }
    $candidate = Get-ChildItem -LiteralPath $resolved -Directory |
        Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json") } |
        Sort-Object Name -Descending | Select-Object -First 1
    if ($null -eq $candidate) {
        throw "В локальном источнике нет backup run с manifest.json."
    }
    return @{ RunId = $candidate.Name; Remote = $false; SourcePath = $candidate.FullName }
}

function Copy-BackupToRuntime {
    param([hashtable]$Backup)
    New-Item -ItemType Directory -Force -Path $script:DownloadsRoot | Out-Null
    $destination = Join-Path $script:DownloadsRoot $Backup.RunId
    if (Test-Path -LiteralPath (Join-Path $destination "manifest.json") -PathType Leaf) {
        return $destination
    }
    if (Test-Path -LiteralPath $destination) {
        Remove-DirectorySafely -Path $destination -AllowedRoot $script:DownloadsRoot
    }
    $temporary = Join-Path $script:DownloadsRoot (
        ".{0}.partial-{1}" -f $Backup.RunId, [Guid]::NewGuid().ToString("N")
    )
    New-Item -ItemType Directory -Path $temporary | Out-Null
    try {
        if ($Backup.Remote) {
            & rclone copy $Backup.SourcePath $temporary
            if ($LASTEXITCODE -ne 0) {
                throw "rclone download завершился с ошибкой."
            }
        }
        else {
            Copy-Item -Path (Join-Path $Backup.SourcePath "*") -Destination $temporary -Recurse
        }
        if (-not (Test-Path -LiteralPath (Join-Path $temporary "manifest.json") -PathType Leaf)) {
            throw "Скачанный backup не содержит manifest.json."
        }
        Move-Item -LiteralPath $temporary -Destination $destination
    }
    catch {
        Remove-DirectorySafely -Path $temporary -AllowedRoot $script:DownloadsRoot
        throw
    }
    return $destination
}

function Remove-OldDownloads {
    param([int]$Keep)
    if ($Keep -lt 1 -or -not (Test-Path -LiteralPath $script:DownloadsRoot)) {
        return
    }
    $old = Get-ChildItem -LiteralPath $script:DownloadsRoot -Directory |
        Where-Object { $_.Name -notmatch "^\." } |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip $Keep
    foreach ($directory in $old) {
        Remove-DirectorySafely -Path $directory.FullName -AllowedRoot $script:DownloadsRoot
    }
}

function Show-Status {
    Set-TargetFromControl | Out-Null
    Start-Database
    Invoke-Manage -ManageArgs @("emergency_status") | Out-Null
    Show-StandbyRefreshWarning
}

function Sync-Standby {
    param([hashtable]$Environment)
    $control = Get-ControlState
    if ($null -ne $control -and $null -ne $control.offline_lifecycle) {
        throw "Standby sync запрещён: offline lifecycle уже начат."
    }
    $backup = $null
    $manifest = $null
    $previousRelease = $null
    try {
        $source = $Environment["DENSTOCK_EMERGENCY_BACKUP_SOURCE"]
        if (-not $source -or $source.StartsWith("REPLACE-")) {
            throw "DENSTOCK_EMERGENCY_BACKUP_SOURCE не настроен."
        }
        $backup = Get-LatestBackup -Source $source
        $download = Copy-BackupToRuntime -Backup $backup
        $manifest = Get-BackupManifest -RunPath $download
        $previousRelease = Set-ReleaseForBackup -Environment $Environment -Manifest $manifest
        Assert-ReleaseIdentity -Environment $Environment
        Set-ControlTarget
        Start-Database
        $containerSource = "/app/.emergency/downloads"
        Invoke-Manage -ManageArgs @(
            "emergency_sync", "--source", $containerSource, "--run-id", $backup.RunId
        ) | Out-Null
        Write-StandbyRefreshStatus -Outcome "ready" -RunId $backup.RunId -AppCommit $manifest.app_commit
    }
    catch {
        Restore-PreviousRelease -Environment $Environment -Previous $previousRelease
        $runId = if ($null -ne $backup) { [string]$backup.RunId } else { "" }
        $commit = if ($null -ne $manifest) { [string]$manifest.app_commit } else { "" }
        $outcome = if ($commit -and $commit -ne [string]$Environment["DENSTOCK_APP_COMMIT"]) {
            "incompatible"
        }
        else {
            "failed"
        }
        Write-StandbyRefreshStatus -Outcome $outcome -RunId $runId -AppCommit $commit -Code "sync_failed"
        throw
    }
    $keep = 2
    if ($Environment["DENSTOCK_EMERGENCY_KEEP_DOWNLOADS"] -match "^\d+$") {
        $keep = [Math]::Max(1, [int]$Environment["DENSTOCK_EMERGENCY_KEEP_DOWNLOADS"])
    }
    Remove-OldDownloads -Keep $keep
    Write-OperatorLog -Event "standby_sync" -Outcome "success" -Details $backup.RunId
}

function Start-Offline {
    param([hashtable]$Environment)
    Assert-ReleaseIdentity -Environment $Environment
    $control = Set-TargetFromControl
    if ($null -eq $control -or $null -eq $control.active_standby) {
        throw "Проверенной standby-копии нет."
    }
    if ($null -ne $control.offline_lifecycle) {
        $lifecycleStatus = [string]$control.offline_lifecycle.status
        if ($lifecycleStatus -notin @("starting", "active")) {
            throw "Offline lifecycle уже находится в состоянии $lifecycleStatus."
        }
        $resumePhrase = "НАЧАТЬ-АВТОНОМНУЮ-РАБОТУ"
        Confirm-Exact -Phrase $resumePhrase -Prompt (
            "Обнаружен прерванный запуск. Будет восстановлен существующий lifecycle marker."
        )
        Start-Database
        $resumeKind = [string]$control.offline_lifecycle.kind
        if ($resumeKind -notin @("planned", "unplanned")) {
            $resumeKind = "unplanned"
        }
        Invoke-Manage -ManageArgs @(
            "emergency_start", "--kind", $resumeKind, "--confirm", $resumePhrase,
            "--resume"
        ) | Out-Null
        Invoke-EmergencyCompose -CommandArgs @(
            "up", "-d", "--build", "emergency-web", "emergency-proxy"
        ) | Out-Null
        Write-OperatorLog -Event "offline_start" -Outcome "resumed"
        return
    }
    Write-Host "Backup time: $($control.active_standby.backup_created_at)"
    Write-Host "Production commit: $($control.active_standby.app_commit)"
    Write-Host "Backup run: $($control.active_standby.backup_run_id)"
    $kindAnswer = Read-Host "Тип запуска: 1 - planned, 2 - unplanned"
    $kind = if ($kindAnswer -eq "1") { "planned" } elseif ($kindAnswer -eq "2") { "unplanned" } else { throw "Неизвестный тип запуска." }
    if ([string]$Environment["DENSTOCK_EMERGENCY_ROLE"] -ne "primary") {
        throw "Этот компьютер настроен как cold standby secondary и не может быть emergency writer."
    }
    $productionUrl = [string]$Environment["DENSTOCK_PRODUCTION_URL"]
    if ($kind -eq "unplanned" -and $productionUrl) {
        try {
            $response = Invoke-WebRequest -Uri "$($productionUrl.TrimEnd('/'))/healthz/" -TimeoutSec 5 -UseBasicParsing
            if ($response.StatusCode -eq 200) {
                throw "Production доступен. Unplanned activation заблокирован; не создавайте второй writer."
            }
        }
        catch [System.Management.Automation.RuntimeException] {
            throw
        }
        catch {
            # Network failure is exactly the condition for an unplanned session.
        }
    }
    $phrase = "НАЧАТЬ-АВТОНОМНУЮ-РАБОТУ"
    Confirm-Exact -Phrase $phrase -Prompt (
        "После запуска все складские операции должны выполняться только на этом компьютере."
    )
    Start-Database
    Invoke-Manage -ManageArgs @(
        "emergency_start", "--kind", $kind, "--confirm", $phrase
    ) | Out-Null
    Invoke-EmergencyCompose -CommandArgs @(
        "up", "-d", "--build", "emergency-web", "emergency-proxy"
    ) | Out-Null
    Write-OperatorLog -Event "offline_start" -Outcome "success" -Details $kind
}

function Stop-Offline {
    $control = Set-TargetFromControl
    $phrase = "ЗАВЕРШИТЬ-И-ЗАМОРОЗИТЬ"
    Confirm-Exact -Phrase $phrase -Prompt (
        "Новые записи будут запрещены, затем будет создан полный local export."
    )
    Start-Database
    $arguments = @("emergency_stop", "--confirm", $phrase)
    if (
        $null -ne $control -and
        $null -ne $control.offline_lifecycle -and
        [string]$control.offline_lifecycle.status -eq "freezing"
    ) {
        $arguments += "--resume"
    }
    Invoke-Manage -ManageArgs $arguments | Out-Null
    Write-OperatorLog -Event "offline_stop" -Outcome "success"
}

function Check-Failback {
    param([hashtable]$Environment)
    Set-TargetFromControl | Out-Null
    $productionUrl = $Environment["DENSTOCK_PRODUCTION_URL"]
    if (-not $productionUrl) {
        throw "DENSTOCK_PRODUCTION_URL не настроен."
    }
    Start-Database
    $exitCode = Invoke-Manage -ManageArgs @(
        "emergency_failback_check", "--production-url", $productionUrl
    ) -AllowFailure
    $outcome = if ($exitCode -eq 0) { "eligible" } else { "blocked_or_conflict" }
    Write-OperatorLog -Event "failback_check" -Outcome $outcome
}

function Prepare-Package {
    Set-TargetFromControl | Out-Null
    $phrase = "ПОДГОТОВИТЬ-ПАКЕТ"
    Confirm-Exact -Phrase $phrase -Prompt (
        "Будет создан только локальный пакет. Upload и production restore не выполняются."
    )
    Start-Database
    Invoke-Manage -ManageArgs @(
        "emergency_prepare_failback", "--confirm", $phrase
    ) | Out-Null
    Write-OperatorLog -Event "failback_package" -Outcome "success"
    Start-Process explorer.exe -ArgumentList (Join-Path $script:RuntimeRoot "packages")
}

function Complete-Failback {
    param([hashtable]$Environment)
    Set-TargetFromControl | Out-Null
    $productionUrl = $Environment["DENSTOCK_PRODUCTION_URL"]
    if (-not $productionUrl) {
        throw "DENSTOCK_PRODUCTION_URL не настроен."
    }
    $phrase = "ПОДТВЕРДИТЬ-ЗАВЕРШЕННЫЙ-FAILBACK"
    Confirm-Exact -Phrase $phrase -Prompt (
        "Используйте только после production restore и успешного production finalizer."
    )
    Start-Database
    Invoke-Manage -ManageArgs @(
        "emergency_complete_failback", "--production-url", $productionUrl,
        "--confirm", $phrase
    ) | Out-Null
    Write-OperatorLog -Event "failback_complete" -Outcome "success"
}

function Remove-CompletedArtifacts {
    Set-TargetFromControl | Out-Null
    $phrase = "УДАЛИТЬ-СТАРЫЕ-COMPLETED-КОПИИ"
    Confirm-Exact -Phrase $phrase -Prompt (
        "Удаляются только старые копии с подтверждённым COMPLETED. Минимум одна сохранится."
    )
    Start-Database
    Invoke-Manage -ManageArgs @(
        "emergency_prune", "--confirm", $phrase
    ) | Out-Null
    Write-OperatorLog -Event "retention" -Outcome "success"
}

function Open-Local {
    param([hashtable]$Environment)
    Assert-ReleaseIdentity -Environment $Environment
    Set-TargetFromControl | Out-Null
    Start-Database
    Invoke-EmergencyCompose -CommandArgs @(
        "up", "-d", "--build", "emergency-web", "emergency-proxy"
    ) | Out-Null
    $port = if ($Environment["DENSTOCK_EMERGENCY_PORT"]) {
        $Environment["DENSTOCK_EMERGENCY_PORT"]
    }
    else {
        "8080"
    }
    Start-Process "http://localhost:$port"
}

function Invoke-SelectedAction {
    param([string]$Selected, [hashtable]$Environment)
    switch ($Selected) {
        "Status" { Show-Status }
        "Sync" { Sync-Standby -Environment $Environment }
        "Start" { Start-Offline -Environment $Environment }
        "Stop" { Stop-Offline }
        "FailbackCheck" { Check-Failback -Environment $Environment }
        "Package" { Prepare-Package }
        "Complete" { Complete-Failback -Environment $Environment }
        "Prune" { Remove-CompletedArtifacts }
        "Open" { Open-Local -Environment $Environment }
        default { throw "Неизвестное действие: $Selected" }
    }
}

try {
    $environment = Read-DotEnv
    $script:Environment = $environment
    Initialize-EmergencyRuntime -Environment $environment
    Assert-Prerequisites
    if ($Action -ne "Menu") {
        Invoke-SelectedAction -Selected $Action -Environment $environment
        exit 0
    }
    while ($true) {
        Write-Host ""
        Write-Host "DenisStock Emergency Control" -ForegroundColor Cyan
        Write-Host "1. Статус"
        Write-Host "2. Обновить аварийную копию"
        Write-Host "3. Запустить автономный режим"
        Write-Host "4. Завершить автономную работу"
        Write-Host "5. Проверить возможность возврата на сервер"
        Write-Host "6. Подготовить пакет для возврата или reconciliation"
        Write-Host "7. Открыть локальный DenisStock"
        Write-Host "8. Подтвердить завершённый возврат и разрешить следующий sync"
        Write-Host "9. Удалить старые подтверждённые копии"
        Write-Host "0. Выход"
        $choice = Read-Host "Выберите действие"
        $selected = switch ($choice) {
            "1" { "Status" }
            "2" { "Sync" }
            "3" { "Start" }
            "4" { "Stop" }
            "5" { "FailbackCheck" }
            "6" { "Package" }
            "7" { "Open" }
            "8" { "Complete" }
            "9" { "Prune" }
            "0" { return }
            default { Write-Host "Неизвестный пункт." -ForegroundColor Yellow; continue }
        }
        try {
            Invoke-SelectedAction -Selected $selected -Environment $environment
        }
        catch {
            Write-OperatorLog -Event $selected -Outcome "failed" -Details $_.Exception.Message
            Write-Host $_.Exception.Message -ForegroundColor Red
        }
    }
}
catch {
    Write-OperatorLog -Event $Action -Outcome "failed" -Details $_.Exception.Message
    Write-Host $_.Exception.Message -ForegroundColor Red
    exit 1
}
