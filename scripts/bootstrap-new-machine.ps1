[CmdletBinding()]
param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$SkipInstall,
    [switch]$SkipMigrate,
    [switch]$RequireDocker
)

$ErrorActionPreference = "Stop"

function Fail-Bootstrap([string]$Message) {
    throw "DenisStock bootstrap: $Message"
}

function Require-Command([string]$Name, [string]$InstallHint) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        Fail-Bootstrap "$Name is required. $InstallHint"
    }
}

function Get-PythonCommand {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return @("py", "-3")
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return @("python")
    }
    Fail-Bootstrap "Python 3.12+ is required. Install it from python.org and rerun."
}

if ($env:OS -ne "Windows_NT") {
    Fail-Bootstrap "This bootstrap script is for Windows. Follow the documented manual setup on another OS."
}

$ProjectRoot = [System.IO.Path]::GetFullPath($ProjectRoot)
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot ".git"))) {
    Fail-Bootstrap "ProjectRoot must be a Git clone, not a production checkout or copied source tree."
}
if ($ProjectRoot.Replace("\", "/").ToLowerInvariant() -like "*/opt/denstock*") {
    Fail-Bootstrap "Refusing a production-like target."
}

Require-Command git "Install Git for Windows, then rerun."
if ($RequireDocker) {
    Require-Command docker "Install Docker Desktop, start it, then rerun with -RequireDocker."
    & docker version --format '{{.Server.Version}}' | Out-Null
}

$python = Get-PythonCommand
$pythonPrefix = @($python | Select-Object -Skip 1)
$versionText = (& $python[0] @pythonPrefix --version 2>&1 | Out-String)
if ($versionText -notmatch "Python 3\.(1[2-9]|[2-9][0-9])") {
    Fail-Bootstrap "Python 3.12+ is required; found $versionText"
}

Set-Location $ProjectRoot
$venv = Join-Path $ProjectRoot ".venv"
if (-not (Test-Path -LiteralPath $venv)) {
    & $python[0] @pythonPrefix -m venv $venv
}
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    Fail-Bootstrap "Virtual environment was not created successfully."
}

$envFile = Join-Path $ProjectRoot ".env"
if (-not (Test-Path -LiteralPath $envFile)) {
    $secret = & $venvPython -c "import secrets; print(secrets.token_urlsafe(48))"
    @(
        "DJANGO_SETTINGS_MODULE=config.settings.dev"
        "DJANGO_SECRET_KEY=$secret"
        "DJANGO_DEBUG=true"
        "DENSTOCK_MODE=development"
        "DJANGO_ALLOWED_HOSTS=127.0.0.1,localhost"
        "DJANGO_SECURE_COOKIES=false"
        "AI_SUPPORT_ENABLED=false"
        "AI_SUPPORT_PROVIDER=disabled"
        "AI_SUPPORT_CODEX_LAUNCH_MODE=disabled"
    ) | Set-Content -LiteralPath $envFile -Encoding utf8
} else {
    Write-Host ".env already exists; preserving it without reading or changing it."
}

if (-not $SkipInstall) {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -e ".[dev]"
}
if (-not $SkipMigrate) {
    & $venvPython manage.py migrate --noinput
}
& $venvPython manage.py check
Write-Host "READY: local development environment is prepared at $ProjectRoot"
