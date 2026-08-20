"""Документы установки должны вести к делу, а не пересказывать архитектуру.

Проверяется то, что дороже всего стоит на месте у компьютера: точные команды,
точный отпечаток ключа, точная фраза подтверждения и запрет на приватный ключ.
Опечатка в любом из них означает сорванный выезд.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
KIT = (ROOT / "docs" / "operations" / "emergency-install-kit.md").read_text(encoding="utf-8")
ONE_PAGE_PATH = ROOT / "docs" / "operations" / "emergency-denis-one-page.md"
ONE_PAGE = ONE_PAGE_PATH.read_text(encoding="utf-8")
OPS = ROOT / "scripts" / "operations"

PRODUCTION_FINGERPRINT = "5615837ef355d2d1881508434980efac31f1c467acb3d31c57101ced3ee5d5b1"


def test_the_kit_names_every_script_it_tells_you_to_run():
    """Документ не должен ссылаться на сценарий, которого нет."""
    for script in (
        "Test-DenisStockEmergencyPreflight.ps1",
        "Install-DenisStock-EmergencyWorkstation.ps1",
        "DenisStock-Emergency.ps1",
        "Test-DenisStockEmergency.ps1",
        "Collect-DenisStockEmergencyDiagnostics.ps1",
    ):
        assert script in KIT, f"в инструкции нет сценария {script}"
        assert (OPS / script).is_file(), f"сценарий {script} упомянут, но отсутствует"


def test_the_kit_carries_the_exact_fingerprint_to_compare():
    assert PRODUCTION_FINGERPRINT in KIT
    installer_path = OPS / "Install-DenisStock-EmergencyWorkstation.ps1"
    installer = installer_path.read_text(encoding="utf-8-sig")
    assert PRODUCTION_FINGERPRINT in installer, "отпечаток в инструкции и в установщике разошёлся"


def test_the_kit_carries_the_exact_authorization_phrase():
    """Фраза подтверждения проверяется командой посимвольно."""
    command = (
        ROOT / "apps" / "operations" / "management" / "commands" / "authorize_emergency_primary.py"
    ).read_text(encoding="utf-8")
    assert "НАЗНАЧИТЬ-EMERGENCY-PRIMARY" in command
    assert "НАЗНАЧИТЬ-EMERGENCY-PRIMARY" in KIT, "в инструкции другая фраза подтверждения"
    assert "authorize_emergency_primary" in KIT


def test_the_kit_states_that_the_private_key_never_travels():
    assert "Приватный ключ подписи на станцию не привозится" in KIT
    for forbidden in ("production-ed25519.key", "-----BEGIN PRIVATE KEY-----"):
        assert forbidden not in KIT, f"инструкция ссылается на приватный ключ: {forbidden}"


def test_the_kit_tells_the_operator_to_stop_on_a_wrong_fingerprint():
    assert "Остановиться" in KIT
    assert "не совпал" in KIT


def test_the_kit_has_a_checklist_and_a_failure_table():
    assert "[ ] Идентификатор станции записан" in KIT
    assert "Что делать, если пошло не так" in KIT


def test_the_kit_does_not_promise_unmeasured_timings():
    """Времени на настоящем компьютере ещё не мерили, обещать его нельзя."""
    assert "Замеров на настоящем компьютере склада ещё не было" in KIT


def test_the_one_page_is_short_and_free_of_commands():
    assert len(ONE_PAGE.splitlines()) < 90, "страница для сотрудника разрослась"
    for technical in ("powershell", "docker", "wsl", ".ps1", "manage.py", "http://"):
        assert technical not in ONE_PAGE.lower(), f"в странице сотрудника техника: {technical}"


def test_the_one_page_covers_all_four_states():
    assert "Обычный день" in ONE_PAGE
    assert "АВТОНОМНЫЙ РЕЖИМ" in ONE_PAGE
    assert "Записи остановлены" in ONE_PAGE
    assert "Сначала сообщите администратору" in ONE_PAGE


def test_the_one_page_explains_why_a_single_writer_matters():
    """Сотруднику нужна причина, иначе запрет выглядит произволом."""
    assert "только одна система" in ONE_PAGE


def test_the_kit_explains_the_separate_read_only_storage_account():
    """Ключ выгрузки копий с production на станцию переносить нельзя."""
    assert "только на чтение" in KIT
    assert "не переносятся никогда" in KIT
    assert "rclone config" in KIT
    assert "yandex-s3" in KIT


def test_the_kit_forbids_leaking_the_storage_key():
    for rule in ("снимки экрана", "не передаётся сценариям параметром"):
        assert rule in KIT, f"в инструкции нет правила: {rule}"


def test_the_kit_states_the_source_check_writes_nothing():
    assert "ничего в хранилище не создаёт и не удаляет" in KIT
    assert "Пробного объекта не появляется" in KIT


def test_the_kit_checks_the_source_before_install():
    """Иначе станция встанет готовой, но забирать копии ей будет неоткуда."""
    assert "-BackupSource" in KIT
    assert "до** установки" in KIT


def test_the_kit_points_at_the_single_readiness_command():
    assert "Что делать дальше" in KIT
    assert "ГОТОВО: станция готова к работе." in KIT


def test_the_kit_carries_no_real_credentials():
    """В документе не должно быть ни ключа, ни его половины."""
    import re
    for pattern in (r"AKIA[0-9A-Z]{8,}", r"YC[A-Za-z0-9_-]{20,}",
                    r"secret_access_key\s*=\s*\S", r"access_key_id\s*=\s*\S"):
        assert not re.search(pattern, KIT), f"в инструкции похоже на ключ: {pattern}"


INSTALLER_PATH = OPS / "Install-DenisStock-EmergencyWorkstation.ps1"
CHECKLIST_PATH = ROOT / "docs" / "operations" / "emergency-physical-install-checklist.md"
CHECKLIST = CHECKLIST_PATH.read_text(encoding="utf-8")


def test_the_checklist_names_only_scripts_that_exist():
    """Опечатка в имени сценария означает сорванный день установки."""
    import re
    for name in sorted(set(re.findall(r"[A-Za-z][A-Za-z-]+\.ps1", CHECKLIST))):
        assert (OPS / name).is_file(), f"в листе указан несуществующий сценарий: {name}"


def test_the_checklist_uses_real_installer_parameters():
    """Параметр, которого нет, остановит установку на месте."""
    installer = INSTALLER_PATH.read_text(encoding="utf-8-sig")
    # Блок параметров заканчивается перед первой настройкой сценария.
    declared = installer.split("$ErrorActionPreference", 1)[0]
    for parameter in ("-RepoRoot", "-InstallWslRuntime", "-CreateTasks", "-BackupSource",
                      "-ProductionUrl", "-PrimaryLanAddress", "-AppCommit", "-ReleaseSource",
                      "-ManifestPublicKeyPath", "-ManifestSigningKeyId", "-ConfirmPrimary"):
        assert parameter in CHECKLIST, f"в листе нет параметра {parameter}"
        assert parameter.lstrip("-") in declared, f"установщик не принимает {parameter}"


def test_the_checklist_uses_real_launcher_actions():
    launcher = (OPS / "DenisStock-Emergency.ps1").read_text(encoding="utf-8-sig")
    assert "-Action Sync" in CHECKLIST
    assert '"Sync"' in launcher


def test_the_checklist_carries_the_exact_authorization_phrase():
    command = (
        ROOT / "apps" / "operations" / "management" / "commands" / "authorize_emergency_primary.py"
    ).read_text(encoding="utf-8")
    assert "НАЗНАЧИТЬ-EMERGENCY-PRIMARY" in command
    assert "НАЗНАЧИТЬ-EMERGENCY-PRIMARY" in CHECKLIST


def test_the_checklist_carries_the_exact_fingerprint():
    installer = INSTALLER_PATH.read_text(encoding="utf-8-sig")
    assert PRODUCTION_FINGERPRINT in CHECKLIST
    assert PRODUCTION_FINGERPRINT in installer


def test_the_checklist_has_no_real_secrets():
    import re
    for pattern in (r"AKIA[0-9A-Z]{8,}", r"YC[A-Za-z0-9_-]{20,}",
                    r"secret_access_key\s*=\s*\S", r"-----BEGIN"):
        assert not re.search(pattern, CHECKLIST), f"в листе похоже на секрет: {pattern}"


def test_the_checklist_checks_the_task_account_on_site():
    """Именно это молчаливо ломается: задание под чужим профилем."""
    assert "Principal.UserId" in CHECKLIST
    assert "S4U" in CHECKLIST


def test_the_checklist_forbids_activation_on_install_day():
    assert "Автономный режим не включаем" in CHECKLIST
    assert "Приватный ключ подписи на станцию не привозим" in CHECKLIST


def test_the_checklist_keeps_the_stale_primary_limitation():
    assert "может включиться автономно" in CHECKLIST
    assert "Отозвать назначение старой" in CHECKLIST


def test_the_checklist_stays_a_checklist_and_not_an_essay():
    """Сторож по соотношению, а не по числу строк.

    Лист покрывает два компьютера и весь день установки, поэтому длина сама по
    себе ни о чём не говорит. Важно другое: он должен оставаться перечнем
    действий. Пояснения нужны там, где шаг связан с безопасностью, но их не
    должно становиться больше самих шагов вдвое.
    """
    lines = CHECKLIST.splitlines()
    steps = [line for line in lines if line.strip().startswith("[ ]")]
    prose = [
        line for line in lines
        if line.strip()
        and not line.strip().startswith("[ ]")
        and not line.startswith("#")
        and not line.startswith("```")
        and not line.startswith("|")
    ]
    assert len(steps) >= 30, f"это уже не лист выполнения: шагов всего {len(steps)}"
    assert len(prose) <= 2 * len(steps), (
        f"пояснений {len(prose)} при {len(steps)} шагах: лист превращается в рассказ"
    )


def test_the_checklist_uses_the_canonical_health_endpoint():
    """Корень отвечает перенаправлением на вход: это не проверка здоровья."""
    urls = (ROOT / "apps" / "core" / "urls.py").read_text(encoding="utf-8")
    assert 'path("healthz/"' in urls, "канонический адрес проверки изменился"
    assert "/healthz/" in CHECKLIST, "в листе нет канонического адреса проверки"


def test_the_checklist_keeps_the_signing_compose_override():
    """Файл не отслеживается Git и легко теряется при неаккуратной выкладке."""
    assert "docker-compose.signing.yml" in CHECKLIST
    assert "Ничего не удаляйте" in CHECKLIST


def test_the_checklist_never_removes_untracked_files_on_production():
    for forbidden in ("git clean", "rm -rf /opt", "git reset --hard"):
        assert forbidden not in CHECKLIST, f"в листе разрушающая команда: {forbidden}"


def test_the_checklist_recreates_only_the_web_service():
    assert "--no-deps web" in CHECKLIST
    assert "PostgreSQL не трогаем" in CHECKLIST


def test_the_checklist_explains_when_the_probe_token_is_needed():
    """Установщик просит его сразу, а нужен он только при возврате."""
    assert "probe token" in CHECKLIST
    assert "возврате с автономного режима" in CHECKLIST
    assert "не передавать в параметрах команды" in CHECKLIST.replace("нельзя ", "не ")


def test_the_checklist_includes_the_credential_hardening_step():
    assert "Protect-DenisStockEmergencyCredentials.ps1" in CHECKLIST
    assert (OPS / "Protect-DenisStockEmergencyCredentials.ps1").is_file()
    assert "-WhatIf" in CHECKLIST, "нет предварительного показа перед изменением прав"
