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
