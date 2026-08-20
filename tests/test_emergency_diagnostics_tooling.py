"""Диагностика станции и сбор сведений для разработчика.

Обычный статус спрашивает само приложение и потому замолкает, когда сломан слой
под ним: не поднялась подсистема Linux, не запущен Docker, нет контейнера.
Диагностика идёт снизу вверх и называет самый нижний сломанный слой.

Набор для отправки разработчику обязан быть безопасным: пароль базы, ключ
Django, probe token и содержимое ключей в него попадать не должны.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
DIAGNOSTICS = (OPS / "Test-DenisStockEmergency.ps1").read_text(encoding="utf-8-sig")
COLLECT = (OPS / "Collect-DenisStockEmergencyDiagnostics.ps1").read_text(encoding="utf-8-sig")


def test_diagnostics_checks_every_layer_bottom_up():
    for layer in ("Windows", "Подсистема Linux", "Docker", "Конфигурация",
                  "Идентификатор станции", "Доверенный ключ", "Сеть", "DenisStock"):
        assert layer in DIAGNOSTICS, f"диагностика не покрывает слой: {layer}"


def test_diagnostics_does_not_cascade_when_a_lower_layer_is_broken():
    """Сломанный Docker не должен превращаться в отказ всех проверок выше."""
    assert "НЕТ ДАННЫХ" in DIAGNOSTICS
    assert "не проверялся: не работает Docker" in DIAGNOSTICS
    assert "не проверялся: не работает подсистема Linux" in DIAGNOSTICS


def test_diagnostics_names_the_lowest_broken_layer():
    assert "Разбирайтесь снизу вверх, начиная со слоя" in DIAGNOSTICS


def test_diagnostics_reads_the_wsl_list_in_its_own_encoding():
    assert "[Text.Encoding]::Unicode" in DIAGNOSTICS


def test_diagnostics_notices_a_changed_lan_address():
    """Адрес мог смениться: тогда ярлыки на других компьютерах перестают работать."""
    assert "больше не принадлежит компьютеру" in DIAGNOSTICS


def test_diagnostics_changes_nothing():
    for forbidden in ("New-NetFirewallRule", "Register-ScheduledTask", "Set-Acl",
                      "Remove-Item", "wsl.exe --install", "docker compose up"):
        assert forbidden not in DIAGNOSTICS, f"диагностика изменяет систему: {forbidden}"


def test_the_bundle_lists_safe_settings_explicitly():
    """Разрешительный список: неизвестное имя скрывается, а не публикуется."""
    assert "$SafeSettingNames" in COLLECT
    assert "<скрыто>" in COLLECT
    for safe in ("DENSTOCK_MODE", "DENSTOCK_APP_COMMIT", "DENSTOCK_MANIFEST_SIGNING_KEY_ID"):
        assert safe in COLLECT
    for secret in ("POSTGRES_PASSWORD", "DJANGO_SECRET_KEY", "DENSTOCK_EMERGENCY_PROBE_TOKEN"):
        assert f'"{secret}",' not in COLLECT, f"{secret} попал в список безопасных"


def test_the_bundle_refuses_to_ship_when_a_secret_slipped_in():
    """Последняя проверка перед архивом: нашли секрет - архив не создаётся."""
    assert "Сбор остановлен: в набор попали секреты" in COLLECT
    assert "Remove-Item -LiteralPath $staging -Recurse -Force" in COLLECT
    for pattern in ("BEGIN PRIVATE KEY", "POSTGRES_PASSWORD=[^<]", "DATABASE_URL=[^<]"):
        assert pattern in COLLECT


def test_the_bundle_carries_only_the_key_fingerprint():
    assert "pinned_public_key_fingerprint" in COLLECT
    assert "Copy-Item -LiteralPath $pinnedKey" not in COLLECT, "публичный ключ копируется в архив"


def test_the_bundle_never_collects_warehouse_data():
    for forbidden in ("pg_dump", "manage.py dumpdata", "db.dump", "media.tar.gz", "SELECT "):
        assert forbidden not in COLLECT, f"в набор собираются данные склада: {forbidden}"
