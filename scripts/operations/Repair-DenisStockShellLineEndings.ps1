<#
    .SYNOPSIS
    Возвращает сценариям Linux родные окончания строк в уже существующей копии.

    .DESCRIPTION
    Git на Windows обычно настроен переводить окончания строк при получении
    файлов. Правило в .gitattributes закрывает это на будущее, но уже лежащий
    на диске файл оно само не переписывает: Git считает рабочую копию
    актуальной и просто её не трогает. Проверено на копии склада - после
    получения исправления файл оставался с возвратами каретки, а git status
    показывал чистое дерево.

    Поэтому нужен явный шаг. Он делает ровно одно: для перечисленных Git
    сценариев удаляет файл и тут же получает его обратно из репозитория, уже
    по новому правилу.

    Почему именно так. Обновление времени файла тоже срабатывает, но только
    если между ним и получением никто не заглянул в git status: любая такая
    команда освежает кеш состояния, и получение снова становится пустым.
    Удаление такой хрупкости не имеет.

    Что сценарий НЕ делает:
      - не трогает файлы, которых нет в репозитории;
      - не трогает файлы с несохранёнными правками, а сообщает о них;
      - не сбрасывает ветку, не чистит каталоги, не удаляет рабочее состояние.

    Код возврата: 0, если все сценарии в порядке; 1, если что-то осталось
    сломанным или требует решения человека.
#>
[CmdletBinding()]
param(
    # Каталог с установленным релизом.
    [string]$RepoRoot = "C:\DenisStock",
    # Только проверить и ничего не менять.
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-Git {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $output = & git -C $RepoRoot @Arguments 2>&1
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = @($output) }
}

function Get-TrackedShellScripts {
    <# Список берётся у Git, а не поиском по диску: так под руку не попадёт
       ничего постороннего и ничего не придётся набирать руками. #>
    $result = Invoke-Git -Arguments @("ls-files", "-z", "*.sh")
    if ($result.ExitCode -ne 0) {
        throw "Не удалось получить список сценариев из репозитория $RepoRoot."
    }
    $joined = ($result.Output -join "`n")
    return @($joined -split "`0" | Where-Object { $_ -and $_.Trim() })
}

function Test-HasCarriageReturn {
    param([Parameter(Mandatory)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $bytes = [IO.File]::ReadAllBytes($Path)
    foreach ($byte in $bytes) { if ($byte -eq 13) { return $true } }
    return $false
}

function Get-BrokenShellScripts {
    $broken = @()
    foreach ($relative in Get-TrackedShellScripts) {
        $full = Join-Path $RepoRoot $relative
        if (Test-HasCarriageReturn -Path $full) { $broken += $relative }
    }
    return @($broken)
}

function Test-LocallyModified {
    param([Parameter(Mandatory)][string]$Relative)
    $result = Invoke-Git -Arguments @("status", "--porcelain", "--", $Relative)
    if ($result.ExitCode -ne 0) { return $true }
    return @($result.Output | Where-Object { $_ -and $_.Trim() }).Count -gt 0
}

if (-not (Test-Path -LiteralPath $RepoRoot)) {
    Write-Host "Каталог $RepoRoot не найден." -ForegroundColor Red
    exit 1
}
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Git не найден в PATH." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "Окончания строк в сценариях Linux: $RepoRoot" -ForegroundColor Cyan

# Пустой массив, возвращённый функцией, PowerShell разворачивает в $null,
# и обращение к Count падает под строгим режимом. Обёртка это снимает.
$broken = @(Get-BrokenShellScripts)
if ($broken.Count -eq 0) {
    Write-Host "Все сценарии уже в порядке." -ForegroundColor Green
    exit 0
}

Write-Host ""
Write-Host "Сценариев с возвратом каретки: $($broken.Count)" -ForegroundColor Yellow
foreach ($relative in $broken) { Write-Host "  $relative" }

if ($CheckOnly) {
    Write-Host ""
    Write-Host "Проверка без изменений. Запустите этот же сценарий без -CheckOnly." -ForegroundColor DarkGray
    exit 1
}

$blocked = @()
$repaired = @()

# Наличие несохранённых правок выясняется ДО того, как менять настройку.
#
# Иначе ответ оказывается ложным: с выключенным переводом Git считает
# изменённым любой файл с возвратом каретки, то есть ровно те, ради которых всё
# и затеяно. Измерено: из четырёх сценариев три были пропущены как «с правками»,
# хотя никто их не трогал.
foreach ($relative in $broken) {
    if (Test-LocallyModified -Relative $relative) { $blocked += $relative }
}
$repairable = @($broken | Where-Object { $blocked -notcontains $_ })

# Перевод окончаний строк выключается на время работы и возвращается обратно.
#
# Зачем. Если выпуск уже содержит правило для сценариев, оно и так сильнее
# настройки, и выключение ничего не меняет. Но копия может стоять на выпуске,
# где правила ещё нет: тогда файл, полученный заново, снова окажется с
# возвратом каретки, и шаг оказался бы бесполезным.
#
# Настройка возвращается в исходный вид в любом случае, даже при ошибке. Если
# её оставить выключенной, Git начнёт считать изменёнными все остальные файлы
# рабочей копии, а установщик из такого каталога работать отказывается.
$previous = & git -C $RepoRoot config --local --get core.autocrlf 2>$null
$hadPrevious = ($LASTEXITCODE -eq 0 -and $previous)
& git -C $RepoRoot config --local core.autocrlf false | Out-Null

try {
    foreach ($relative in $repairable) {
        $full = Join-Path $RepoRoot $relative
        Remove-Item -LiteralPath $full -Force
        $checkout = Invoke-Git -Arguments @("checkout", "--", $relative)
        if ($checkout.ExitCode -ne 0) {
            throw "Не удалось восстановить $relative из репозитория. Файл удалён, восстановите его: git -C $RepoRoot checkout -- $relative"
        }
        $repaired += $relative
    }
}
finally {
    if ($hadPrevious) {
        & git -C $RepoRoot config --local core.autocrlf $previous | Out-Null
    }
    else {
        & git -C $RepoRoot config --local --unset core.autocrlf 2>$null | Out-Null
    }
}

Write-Host ""
if ($repaired.Count -gt 0) {
    Write-Host "Восстановлено: $($repaired.Count)" -ForegroundColor Green
    foreach ($relative in $repaired) { Write-Host "  $relative" }
}

if ($blocked.Count -gt 0) {
    Write-Host ""
    Write-Host "Пропущены, потому что в них есть несохранённые правки:" -ForegroundColor Yellow
    foreach ($relative in $blocked) { Write-Host "  $relative" }
    Write-Host "Сохраните или отмените правки и повторите." -ForegroundColor Yellow
}

$stillBroken = @(Get-BrokenShellScripts)
Write-Host ""
if ($stillBroken.Count -eq 0) {
    Write-Host "Проверка после исправления: возвратов каретки нет." -ForegroundColor Green
    exit 0
}

Write-Host "Осталось сломанных: $($stillBroken.Count)" -ForegroundColor Red
foreach ($relative in $stillBroken) { Write-Host "  $relative" }
exit 1
