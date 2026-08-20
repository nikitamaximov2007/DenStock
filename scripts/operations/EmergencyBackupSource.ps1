<#
    .SYNOPSIS
    Проверка источника резервных копий для аварийной станции. Только чтение.

    .DESCRIPTION
    Файл подключается через точку из проверок готовности и диагностики, чтобы
    обе они судили об источнике одинаково. Самостоятельно ничего не выполняет.
    Здесь же лежат общие вспомогательные функции этих проверок: запуск
    внешней программы с ограничением по времени и путь к настройкам rclone.

    Главное свойство: ни одна функция здесь не пишет в хранилище. Ни пробного
    объекта, ни проверки прав на запись записью. Станция забирает копии и
    ничего в них не меняет, поэтому и учётная запись у неё только на чтение.

    Секреты не печатаются никогда: ни ключ доступа, ни содержимое rclone.conf.
    Наружу отдаются только имя удалённого источника, имена каталогов копий и
    род ошибки.
#>

Set-StrictMode -Version Latest

function Get-BackupSourceKind {
    <#
        Различает путь Windows и источник вида "remote:path" ровно так же, как
        это делает рабочий сценарий синхронизации. Расхождение здесь было бы
        хуже отсутствия проверки: она бы говорила «готово» там, где реальная
        синхронизация не пройдёт.
    #>
    param([string]$Source)
    if (-not $Source) { return "empty" }
    if ($Source -match "^[A-Za-z]:[\\/]") { return "windows-path" }
    if ($Source -match "^[A-Za-z0-9_.-]+:") { return "rclone-remote" }
    return "windows-path"
}

function Split-RcloneSource {
    <#
        Делит "yandex-s3:denstock-backups-nikita/подкаталог" на имя удалённого
        источника и путь внутри него. Имя нужно, чтобы проверить существование
        источника, не читая его настройки.
    #>
    param([string]$Source)
    $index = $Source.IndexOf(":")
    if ($index -lt 1) { return $null }
    return [pscustomobject]@{
        Remote = $Source.Substring(0, $index)
        Path = $Source.Substring($index + 1).Trim("/")
    }
}

function Get-RcloneFailureKind {
    <#
        Определяет род отказа по коду возврата и тексту ошибки.

        Код возврата у rclone задокументирован, поэтому он главный: 3 означает
        «каталог не найден». Дополнительно разбираются идентификаторы ошибок
        S3 вида InvalidAccessKeyId или AccessDenied. Это не разбор английской
        прозы: такие идентификаторы являются частью протокола и не меняются от
        версии к версии и от языка системы.
    #>
    param([int]$ExitCode, [string]$ErrorText)
    $text = [string]$ErrorText

    if ($text -match "InvalidAccessKeyId|SignatureDoesNotMatch|AuthorizationHeaderMalformed|InvalidToken|ExpiredToken") {
        return "auth"
    }
    if ($text -match "AccessDenied|Forbidden|403") { return "access-denied" }
    if ($text -match "NoSuchBucket|bucket .* not found|specified bucket does not exist") { return "source-missing" }
    if ($text -match "didn't find section in config file|didn.t find section|unknown remote") { return "remote-missing" }
    if ($text -match "no such host|dial tcp|i/o timeout|connection refused|TLS handshake|x509|certificate") {
        return "network"
    }
    if ($text -match "RequestTimeTooSkewed") { return "clock-skew" }
    if ($ExitCode -eq 3) { return "source-missing" }
    if ($ExitCode -eq 5) { return "network" }
    return "unknown"
}

function Get-BackupSourceFailureAdvice {
    <#
        Человеческое объяснение и следующий шаг для каждого рода отказа.
        Оператору у компьютера нужен не текст ошибки, а действие.
    #>
    param([string]$Kind, [string]$Remote)
    switch ($Kind) {
        "not-installed" {
            return "rclone не установлен. Поставьте rclone и повторите проверку."
        }
        "remote-missing" {
            return "В rclone нет источника «$Remote». Настройте его командой rclone config под тем же пользователем Windows, от которого работает станция."
        }
        "auth" {
            return "Ключ доступа не принят. Проверьте, что для станции заведён отдельный ключ учётной записи только на чтение и что он введён без лишних пробелов."
        }
        "access-denied" {
            return "Доступ запрещён. У учётной записи станции должно быть право чтения именно этого хранилища копий."
        }
        "source-missing" {
            return "Хранилище или каталог не найдены. Проверьте имя хранилища в источнике копий."
        }
        "network" {
            return "Хранилище недоступно по сети. Проверьте подключение компьютера и доступ к storage.yandexcloud.net."
        }
        "clock-skew" {
            return "Часы компьютера расходятся с хранилищем. Включите синхронизацию времени Windows и повторите."
        }
        "empty" {
            return "Источник доступен, но копий в нём нет. Проверьте, что production создаёт копии и выгружает их в это хранилище."
        }
        default {
            return "Не удалось прочитать источник копий. Соберите набор диагностики и передайте разработчику."
        }
    }
}

function Invoke-RcloneRead {
    <#
        Запускает только читающие подкоманды rclone. Список разрешённых команд
        закрыт намеренно: так в проверку невозможно случайно добавить запись.
    #>
    param([Parameter(Mandatory)][string[]]$Arguments)
    $allowed = @("version", "listremotes", "lsjson", "lsf")
    if ($Arguments.Count -eq 0 -or $allowed -notcontains $Arguments[0]) {
        throw "Проверка источника выполняет только чтение: команда «$($Arguments[0])» не разрешена."
    }
    $stdout = & rclone @Arguments 2>&1
    return [pscustomobject]@{
        ExitCode = $LASTEXITCODE
        Text = ($stdout | Out-String)
    }
}

function Test-EmergencyBackupSource {
    <#
        Полная проверка источника копий, только чтение.

        Возвращает объект с полями State (ГОТОВО/ВНИМАНИЕ/ОСТАНОВКА), Kind,
        Detail, Advice, Remote, LatestRun и Runs. Ничего не печатает: за вывод
        отвечает вызывающий сценарий.
    #>
    param(
        [Parameter(Mandatory)][string]$Source,
        [int]$TimeoutSeconds = 60
    )

    $result = [ordered]@{
        State = "ОСТАНОВКА"; Kind = "unknown"; Detail = ""; Advice = ""
        Remote = ""; Path = ""; LatestRun = ""; Runs = @(); RcloneVersion = ""
    }

    $kind = Get-BackupSourceKind -Source $Source
    if ($kind -eq "empty") {
        $result.Kind = "not-configured"
        $result.Detail = "источник копий не задан"
        $result.Advice = "Укажите источник копий вида yandex-s3:имя-хранилища."
        return [pscustomobject]$result
    }

    # --- Локальный каталог -------------------------------------------------
    if ($kind -eq "windows-path") {
        $result.Remote = "(локальный каталог)"
        $result.Path = $Source
        if (-not (Test-Path -LiteralPath $Source)) {
            $result.Kind = "source-missing"
            $result.Detail = "каталог не найден: $Source"
            $result.Advice = Get-BackupSourceFailureAdvice -Kind "source-missing" -Remote $Source
            return [pscustomobject]$result
        }
        $runs = @(
            Get-ChildItem -LiteralPath $Source -Directory -ErrorAction SilentlyContinue |
                Where-Object { Test-Path -LiteralPath (Join-Path $_.FullName "manifest.json") } |
                ForEach-Object { $_.Name } | Sort-Object -Descending
        )
        $result.Runs = $runs
        if ($runs.Count -eq 0) {
            $result.State = "ОСТАНОВКА"; $result.Kind = "empty"
            $result.Detail = "в каталоге нет копий с manifest.json"
            $result.Advice = Get-BackupSourceFailureAdvice -Kind "empty" -Remote $Source
            return [pscustomobject]$result
        }
        $result.State = "ГОТОВО"; $result.Kind = "ok"
        $result.LatestRun = $runs[0]
        $result.Detail = "копий: $($runs.Count), последняя $($runs[0])"
        return [pscustomobject]$result
    }

    # --- Удалённый источник rclone -----------------------------------------
    $parts = Split-RcloneSource -Source $Source
    if ($null -eq $parts) {
        $result.Kind = "not-configured"
        $result.Detail = "источник не разобран: $Source"
        $result.Advice = "Ожидается вид имя-источника:имя-хранилища."
        return [pscustomobject]$result
    }
    $result.Remote = $parts.Remote
    $result.Path = $parts.Path

    if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
        $result.Kind = "not-installed"
        $result.Detail = "rclone не найден"
        $result.Advice = Get-BackupSourceFailureAdvice -Kind "not-installed" -Remote $parts.Remote
        return [pscustomobject]$result
    }

    $version = Invoke-RcloneRead -Arguments @("version")
    if ($version.ExitCode -eq 0) {
        $first = ($version.Text -split "`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
        $result.RcloneVersion = $first.Trim()
    }

    # Список источников печатает только их имена, настройки в него не попадают.
    $remotes = Invoke-RcloneRead -Arguments @("listremotes")
    $names = @($remotes.Text -split "`n" | ForEach-Object { $_.Trim().TrimEnd(":") } | Where-Object { $_ })
    if ($names -notcontains $parts.Remote) {
        $result.Kind = "remote-missing"
        $result.Detail = "источник «$($parts.Remote)» не настроен" +
            $(if ($names.Count -gt 0) { "; настроены: $($names -join ', ')" } else { "" })
        $result.Advice = Get-BackupSourceFailureAdvice -Kind "remote-missing" -Remote $parts.Remote
        return [pscustomobject]$result
    }

    # Только перечисление каталогов. Ни копирования, ни создания, ни удаления.
    $listing = Invoke-RcloneRead -Arguments @(
        "lsjson", $Source, "--dirs-only", "--no-modtime",
        "--contimeout", "20s", "--timeout", "${TimeoutSeconds}s", "--retries", "1", "--low-level-retries", "2"
    )
    if ($listing.ExitCode -ne 0) {
        $failure = Get-RcloneFailureKind -ExitCode $listing.ExitCode -ErrorText $listing.Text
        $result.Kind = $failure
        $result.Detail = "чтение источника не удалось (код $($listing.ExitCode))"
        $result.Advice = Get-BackupSourceFailureAdvice -Kind $failure -Remote $parts.Remote
        return [pscustomobject]$result
    }

    $runs = @()
    try {
        $entries = $listing.Text | ConvertFrom-Json -ErrorAction Stop
        $runs = @(
            $entries | Where-Object { $_.IsDir } | ForEach-Object { $_.Name } |
                Where-Object { $_ -match "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$" } |
                Sort-Object -Descending
        )
    }
    catch {
        $result.Kind = "unknown"
        $result.Detail = "ответ источника не разобран"
        $result.Advice = Get-BackupSourceFailureAdvice -Kind "unknown" -Remote $parts.Remote
        return [pscustomobject]$result
    }

    $result.Runs = $runs
    if ($runs.Count -eq 0) {
        $result.Kind = "empty"
        $result.Detail = "источник доступен, копий в нём нет"
        $result.Advice = Get-BackupSourceFailureAdvice -Kind "empty" -Remote $parts.Remote
        return [pscustomobject]$result
    }

    $result.State = "ГОТОВО"; $result.Kind = "ok"
    $result.LatestRun = $runs[0]
    $result.Detail = "копий: $($runs.Count), последняя $($runs[0])"
    return [pscustomobject]$result
}

function Get-BackupRunAgeHours {
    <#
        Возраст копии по её имени вида 2026-08-20_08-40-07. Имя задаётся
        production при создании копии, поэтому это надёжнее времени файла,
        которое меняется при выгрузке.
    #>
    param([string]$RunId)
    if ($RunId -notmatch "^(\d{4})-(\d{2})-(\d{2})_(\d{2})-(\d{2})-(\d{2})$") { return $null }
    try {
        $stamp = [datetime]::new(
            [int]$Matches[1], [int]$Matches[2], [int]$Matches[3],
            [int]$Matches[4], [int]$Matches[5], [int]$Matches[6]
        )
    }
    catch { return $null }
    return [math]::Round(((Get-Date) - $stamp).TotalHours, 1)
}

function Get-RcloneConfigPath {
    <#
        Путь к настройкам rclone для ТЕКУЩЕГО пользователя Windows.

        Путь вычисляется по соглашению, а не спрашивается у самого rclone:
        подкоманда config намеренно не входит в список разрешённых, чтобы
        прочитать настройки было невозможно даже случайно. Проверено на живой
        Windows: rclone кладёт файл в %APPDATA%\rclone\rclone.conf, а
        переменная RCLONE_CONFIG перекрывает это место.

        Возвращается только путь и сведения о правах. Содержимое не читается.
    #>
    $override = $env:RCLONE_CONFIG
    if ($override) {
        $path = $override
        $source = "переменная RCLONE_CONFIG"
    }
    else {
        $path = Join-Path $env:APPDATA "rclone" | Join-Path -ChildPath "rclone.conf"
        $source = "обычное место профиля"
    }
    $exists = Test-Path -LiteralPath $path -PathType Leaf
    $result = [ordered]@{
        Path = $path; Source = $source; Exists = $exists
        AclProtected = $null; ExtraReaders = @()
    }
    if ($exists) {
        try {
            $acl = Get-Acl -LiteralPath $path
            $result.AclProtected = $acl.AreAccessRulesProtected
            # Свои, СИСТЕМА и администраторы ожидаемы. Всё остальное - лишние
            # читатели: в файле лежит ключ доступа к хранилищу копий.
            $expected = @(
                [Security.Principal.WindowsIdentity]::GetCurrent().Name,
                "NT AUTHORITY\SYSTEM", "BUILTIN\Administrators"
            )
            $wellKnown = @("S-1-5-18", "S-1-5-32-544")
            $result.ExtraReaders = @(
                $acl.Access | ForEach-Object { [string]$_.IdentityReference } |
                    Where-Object {
                        $identity = $_
                        $known = $false
                        foreach ($name in $expected) { if ($identity -eq $name) { $known = $true } }
                        try {
                            $sid = (New-Object Security.Principal.NTAccount($identity)).Translate(
                                [Security.Principal.SecurityIdentifier]).Value
                            if ($wellKnown -contains $sid) { $known = $true }
                            if ($sid -eq [Security.Principal.WindowsIdentity]::GetCurrent().User.Value) { $known = $true }
                        }
                        catch { }
                        -not $known
                    } | Select-Object -Unique
            )
        }
        catch { }
    }
    return [pscustomobject]$result
}

function Invoke-ExternalWithTimeout {
    <#
        Запускает внешнюю программу с ограничением по времени.

        Нужно потому, что проверка готовности обязана отвечать быстро. На
        машине со сломанной подсистемой Linux вызов wsl.exe -l -q возвращался
        четыре минуты, и человек у компьютера видел молчащее окно, не понимая,
        сломалось что-то или ещё считается. Просроченный вызов честнее ответа
        через четыре минуты.

        Возвращает объект с полями ExitCode, Text и TimedOut. Программа при
        истечении срока снимается.

        В Windows PowerShell 5.1 у ProcessStartInfo нет списка аргументов,
        поэтому строка собирается вручную с кавычками вокруг тех, где есть
        пробелы.
    #>
    param(
        [Parameter(Mandatory)][string]$FilePath,
        [string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 30,
        [switch]$Utf16Output
    )

    $quoted = foreach ($argument in $Arguments) {
        if ($argument -match '\s') { '"' + $argument + '"' } else { $argument }
    }

    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $FilePath
    $info.Arguments = ($quoted -join " ")
    $info.UseShellExecute = $false
    $info.RedirectStandardOutput = $true
    $info.RedirectStandardError = $true
    $info.CreateNoWindow = $true
    if ($Utf16Output) {
        $info.StandardOutputEncoding = [Text.Encoding]::Unicode
        $info.StandardErrorEncoding = [Text.Encoding]::Unicode
    }

    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    $output = New-Object Text.StringBuilder
    $errors = New-Object Text.StringBuilder
    $outHandler = {
        if ($null -ne $EventArgs.Data) { [void]$Event.MessageData.AppendLine($EventArgs.Data) }
    }
    $outSubscription = Register-ObjectEvent -InputObject $process -EventName OutputDataReceived `
        -Action $outHandler -MessageData $output
    $errSubscription = Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived `
        -Action $outHandler -MessageData $errors

    try {
        [void]$process.Start()
        $process.BeginOutputReadLine()
        $process.BeginErrorReadLine()
        $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $finished) {
            try { $process.Kill() } catch { }
            return [pscustomobject]@{
                ExitCode = -1
                Text = ""
                TimedOut = $true
            }
        }
        # Дать обработчикам дочитать остаток вывода после выхода процесса.
        Start-Sleep -Milliseconds 120
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            Text = ($output.ToString() + $errors.ToString())
            TimedOut = $false
        }
    }
    finally {
        Unregister-Event -SubscriptionId $outSubscription.Id -ErrorAction SilentlyContinue
        Unregister-Event -SubscriptionId $errSubscription.Id -ErrorAction SilentlyContinue
        $process.Dispose()
    }
}
