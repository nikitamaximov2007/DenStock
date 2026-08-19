"""Инварианты развёртывания аварийной рабочей станции.

Проверяется не поведение кода, а конфигурация, от которой зависит безопасность
установки: что база данных никогда не выходит в локальную сеть, что правило
межсетевого экрана ограничено локальной подсетью, и что защищаемые файлы теряют
наследование прав и остаются доступны только администратору.

Такую конфигурацию легко ослабить одной строкой при будущей правке, а последствия
обнаружились бы только на складе, поэтому она закреплена тестом.
"""
from __future__ import annotations

import pathlib
import re

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "docker-compose.emergency.yml"
INSTALLER = ROOT / "scripts" / "operations" / "Install-DenisStock-EmergencyWorkstation.ps1"


@pytest.fixture(scope="module")
def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def installer() -> str:
    return INSTALLER.read_text(encoding="utf-8")


# --- Сеть -------------------------------------------------------------------------------


def test_database_service_publishes_no_ports(compose):
    """PostgreSQL обязана оставаться внутри сети контейнеров.

    Публикация порта базы вывела бы её в локальную сеть склада, где к ней смог бы
    подключиться любой компьютер, минуя аутентификацию приложения.
    """
    database = compose["services"]["emergency-db"]
    assert not database.get("ports"), (
        f"база данных публикует порты в сеть: {database.get('ports')!r}"
    )


def test_only_the_proxy_publishes_a_port(compose):
    published = {
        name: service.get("ports")
        for name, service in compose["services"].items()
        if service.get("ports")
    }
    assert set(published) == {"emergency-proxy"}, (
        f"порты наружу публикует не только прокси: {sorted(published)}"
    )


def test_published_port_defaults_to_loopback(compose):
    """По умолчанию станция слушает только себя; выход в сеть задаётся явно."""
    ports = compose["services"]["emergency-proxy"]["ports"]
    assert any("127.0.0.1" in str(entry) for entry in ports), (
        f"привязка по умолчанию не loopback: {ports!r}"
    )


def test_firewall_rule_is_limited_to_the_local_subnet(installer):
    rules = re.findall(r"New-NetFirewallRule[^\n]*", installer)
    assert rules, "установщик не создаёт правило межсетевого экрана"
    for rule in rules:
        assert "-RemoteAddress LocalSubnet" in rule, f"правило открыто шире подсети: {rule}"
        assert "-Direction Inbound" in rule
        assert "-Profile Private" in rule, f"правило действует не только в частной сети: {rule}"


def test_firewall_rule_opens_only_the_application_port(installer):
    """Порт базы данных не должен упоминаться в правилах межсетевого экрана."""
    rules = re.findall(r"New-NetFirewallRule[^\n]*", installer)
    for rule in rules:
        assert "5432" not in rule, f"правило открывает порт PostgreSQL: {rule}"


# --- Права на файлы ----------------------------------------------------------------------


def test_acl_helper_breaks_inheritance(installer):
    """Без разрыва наследования унаследованные разрешения оставили бы доступ всем."""
    assert "SetAccessRuleProtection($true, $false)" in installer, (
        "защищаемые файлы не теряют унаследованные разрешения"
    )


def test_acl_helper_grants_only_administrators_and_the_installer(installer):
    block = installer[installer.index("function Set-AdministratorOnlyAcl") :][:900]
    identities = re.findall(r'"(BUILTIN\\\\?Administrators)"', block)
    assert identities, "администраторы не указаны среди получателей прав"
    assert "Users" not in block and "Everyone" not in block, (
        "в список прав попала широкая группа пользователей"
    )


@pytest.mark.parametrize(
    "protected",
    ["identityFile", "pinnedPublicKey", "trustedKeyDir", "envFile", "runtimeRoot"],
)
def test_every_sensitive_path_gets_the_restricted_acl(installer, protected):
    assert re.search(rf"Set-AdministratorOnlyAcl -Path \${protected}\b", installer), (
        f"путь {protected} остаётся без ограниченных прав"
    )


def test_private_signing_key_is_never_provisioned_to_the_workstation(installer):
    """Приватный ключ подписи живёт только на production."""
    assert "MANIFEST_SIGNING_KEY_PATH" not in installer, (
        "установщик рабочей станции упоминает приватный ключ подписи"
    )
    assert "MANIFEST_PUBLIC_KEY_PATH" in installer, (
        "установщик не закрепляет публичный ключ проверки"
    )


def test_repository_contains_no_signing_key_material():
    """В репозитории не должно быть ни приватного, ни «настоящего» ключа."""
    for path in ROOT.rglob("*.pem"):
        if ".git" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        assert "PRIVATE KEY" not in text, f"в репозитории найден приватный ключ: {path}"
