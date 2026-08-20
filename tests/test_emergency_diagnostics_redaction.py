"""Набор диагностики отправляют разработчику. В нём не должно быть секретов.

Проверка состязательная: в подставную станцию кладутся правдоподобные секреты
всех видов, которые там бывают, набор собирается по-настоящему, и в готовом
архиве их ищут. Чтения исходника здесь мало: вырезка проверяется результатом.

Секреты в этом файле выдуманные и никуда не ведут.
"""
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPS = ROOT / "scripts" / "operations"
COLLECTOR = OPS / "Collect-DenisStockEmergencyDiagnostics.ps1"

POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
needs_powershell = pytest.mark.skipif(POWERSHELL is None, reason="нужен Windows PowerShell")

# Выдуманные значения того же вида, что настоящие.
FAKE_ACCESS_KEY = "YCAJEfakefakefakefakeQ"
FAKE_SECRET_KEY = "YCPfakefakefakefakefakefakefakefakefake"
FAKE_PROBE_TOKEN = "probe-fake-0123456789abcdef0123456789abcdef"
FAKE_DJANGO_KEY = "django-insecure-fakefakefakefakefakefakefakefake"
FAKE_DB_PASSWORD = "fakeDbPassw0rd-not-real"
FAKE_PRIVATE_KEY = (
    "-----BEGIN PRIVATE KEY-----\n"
    "MC4CAQAwBQYDK2VwBCIEIFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeFakeQ\n"
    "-----END PRIVATE KEY-----\n"
)

ALL_SECRETS = (
    FAKE_ACCESS_KEY, FAKE_SECRET_KEY, FAKE_PROBE_TOKEN,
    FAKE_DJANGO_KEY, FAKE_DB_PASSWORD, "BEGIN PRIVATE KEY",
)


def make_station_with_secrets(root: Path) -> None:
    """Станция, где секреты лежат везде, где они бывают в жизни."""
    runtime = root / ".emergency"
    (runtime / "trusted").mkdir(parents=True)
    (runtime / "workstation-id.txt").write_text(
        "7a1c9e40-3b52-4d18-9f6a-8c2e5b7d1049", encoding="utf-8"
    )
    (runtime / "trusted" / "production-manifest-ed25519-public.pem").write_text(
        "-----BEGIN PUBLIC KEY-----\n"
        "MCowBQYDK2VwAyEA5xL0Wl6xJ7v8xQqW0nJ8m4hQ0m9k3Yb2vZ1cF7pXsRA=\n"
        "-----END PUBLIC KEY-----\n",
        encoding="utf-8",
    )
    (root / ".env.emergency").write_text(
        "DENSTOCK_MODE=emergency-local\n"
        "DENSTOCK_EMERGENCY_ROLE=primary\n"
        "DENSTOCK_EMERGENCY_PORT=8080\n"
        "DENSTOCK_EMERGENCY_WSL_DISTRO=Ubuntu\n"
        "DENSTOCK_MANIFEST_SIGNING_KEY_ID=production-1\n"
        f"POSTGRES_PASSWORD={FAKE_DB_PASSWORD}\n"
        f"DJANGO_SECRET_KEY={FAKE_DJANGO_KEY}\n"
        f"DENSTOCK_EMERGENCY_PROBE_TOKEN={FAKE_PROBE_TOKEN}\n"
        f"DATABASE_URL=postgresql://denstock:{FAKE_DB_PASSWORD}@emergency-db:5432/x\n"
        f"AWS_ACCESS_KEY_ID={FAKE_ACCESS_KEY}\n"
        f"AWS_SECRET_ACCESS_KEY={FAKE_SECRET_KEY}\n",
        encoding="utf-8",
    )
    # Приватный ключ там, где его быть не должно, но кто-то мог положить.
    (runtime / "stray-private-key.pem").write_text(FAKE_PRIVATE_KEY, encoding="utf-8")
    (runtime / "standby-refresh-status.json").write_text(
        '{"schema_version":1,"attempted_at":"2026-08-20T07:00:00+03:00",'
        '"outcome":"ready","backup_run_id":"2026-08-20_03-00-17",'
        '"observed_app_commit":"0fcae772eab1da13c1b7b59890827cf9984d3394","code":""}',
        encoding="utf-8",
    )


def collect(root: Path, out_dir: Path) -> Path:
    command = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8; "
        f"& '{COLLECTOR}' -RepoRoot '{root}' -OutputDirectory '{out_dir}'"
    )
    result = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    archives = sorted(out_dir.glob("denstock-emergency-diagnostics-*.zip"))
    assert archives, f"архив не создан: {result.stdout}\n{result.stderr}"
    return archives[-1]


@pytest.fixture(scope="module")
def bundle(tmp_path_factory):
    if POWERSHELL is None:
        pytest.skip("нужен Windows PowerShell")
    root = tmp_path_factory.mktemp("station")
    out = tmp_path_factory.mktemp("out")
    make_station_with_secrets(root)
    return collect(root, out)


@needs_powershell
@pytest.mark.parametrize("secret", ALL_SECRETS)
def test_no_secret_reaches_the_bundle(bundle, secret):
    with zipfile.ZipFile(bundle) as archive:
        for name in archive.namelist():
            text = archive.read(name).decode("utf-8-sig", errors="replace")
            assert secret not in text, f"{name} содержит секрет: {secret[:16]}"


@needs_powershell
def test_the_bundle_still_carries_what_the_developer_needs(bundle):
    """Вырезка не должна превращать набор в пустышку."""
    with zipfile.ZipFile(bundle) as archive:
        joined = "".join(
            archive.read(name).decode("utf-8-sig", errors="replace")
            for name in archive.namelist()
        )
    assert "7a1c9e40-3b52-4d18-9f6a-8c2e5b7d1049" in joined, "нет идентификатора станции"
    assert "production-1" in joined, "нет идентификатора доверенного ключа"
    assert "emergency-local" in joined, "нет режима работы"
    assert "Windows" in joined


@needs_powershell
def test_the_secret_names_are_visible_but_the_values_are_not(bundle):
    """Разработчику полезно знать, что настройка задана, а не что в ней."""
    with zipfile.ZipFile(bundle) as archive:
        settings = archive.read("03-settings.txt").decode("utf-8-sig", errors="replace")
    for name in ("POSTGRES_PASSWORD", "DJANGO_SECRET_KEY", "DENSTOCK_EMERGENCY_PROBE_TOKEN"):
        assert f"{name}=<скрыто>" in settings, f"{name} не скрыт"


@needs_powershell
def test_no_file_of_the_station_runtime_is_copied_wholesale(bundle):
    """Приватный ключ, оказавшийся в каталоге станции, не должен уехать."""
    with zipfile.ZipFile(bundle) as archive:
        names = archive.namelist()
    for name in names:
        assert not name.endswith(".pem"), f"в архив попал файл ключа: {name}"
    assert "stray-private-key.pem" not in " ".join(names)
