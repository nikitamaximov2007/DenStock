"""Что физически уедет на компьютер склада.

Комплект установки не собирается отдельным архивом: на станцию приезжает сам
репозиторий по git, а публичный ключ подписи привозят отдельным файлом. Значит
проверять надо ровно то, что отслеживает git.

Отдельная сборка ZIP была бы вторым путём доставки, который пришлось бы держать
в согласии с первым. Пока установка идёт через git checkout, такой путь только
добавил бы места для расхождения.

Здесь закреплено, что в отслеживаемых файлах нет ничего, чего на складском
компьютере быть не должно.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Значения этого вида в репозитории быть не может. Отпечатки, идентификаторы
# ключей и UUID специально не ловятся: они не секреты.
SECRET_PATTERNS = {
    "приватный ключ": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "ключ доступа AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "статический ключ Yandex": re.compile(r"\bYC[A-Za-z0-9_-]{30,}\b"),
    "секретный ключ хранилища": re.compile(r"secret_access_key\s*=\s*[A-Za-z0-9/+_-]{16,}"),
    "ключ доступа хранилища": re.compile(r"access_key_id\s*=\s*[A-Za-z0-9/+_-]{16,}"),
    "пароль базы": re.compile(r"POSTGRES_PASSWORD\s*=\s*[^\s<${#]{8,}"),
    "ключ Django": re.compile(r"DJANGO_SECRET_KEY\s*=\s*[^\s<${#]{16,}"),
    "probe token": re.compile(r"PROBE_TOKEN\s*=\s*[^\s<${#]{12,}"),
}

# Файлы, которые не должны отслеживаться вовсе.
FORBIDDEN_NAMES = (
    "rclone.conf", ".env", ".env.emergency", "id_rsa", "id_ed25519",
    "production-ed25519.key", "production-signing-key.enc",
)
FORBIDDEN_SUFFIXES = (".key", ".dump", ".sqlite3")


def tracked_files():
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180,
    )
    assert output.returncode == 0, output.stderr
    return [ROOT / name for name in output.stdout.split("\n") if name.strip()]


TRACKED = tracked_files()
TEST_FILES = {p for p in TRACKED if p.parts and "tests" in p.parts}


def test_git_actually_tracks_something():
    assert len(TRACKED) > 100, "список отслеживаемых файлов подозрительно короткий"


@pytest.mark.parametrize("forbidden", FORBIDDEN_NAMES)
def test_no_secret_file_is_tracked_by_name(forbidden):
    hits = [str(p.relative_to(ROOT)) for p in TRACKED if p.name == forbidden]
    assert not hits, f"в репозитории отслеживается {forbidden}: {hits}"


@pytest.mark.parametrize("suffix", FORBIDDEN_SUFFIXES)
def test_no_secret_file_is_tracked_by_extension(suffix):
    allowed = {".env.example", ".env.emergency.example", ".env.backup.example"}
    hits = [
        str(p.relative_to(ROOT))
        for p in TRACKED
        if p.suffix == suffix and p.name not in allowed
    ]
    assert not hits, f"в репозитории отслеживаются файлы {suffix}: {hits}"


# Заглушка в файле-примере не является секретом. Отличается она надёжно: в ней
# нет строчных букв, и она прямо просит себя заменить.
PLACEHOLDER = re.compile(
    r"(REPLACE|ЗАМЕНИТЕ|CHANGE-?ME|EXAMPLE|<[^>]*>|^[A-ZА-Я0-9_-]+$)"
)


def looks_like_a_placeholder(line: str) -> bool:
    value = line.split("=", 1)[1].strip() if "=" in line else line.strip()
    return bool(PLACEHOLDER.search(value))


@pytest.mark.parametrize("label", sorted(SECRET_PATTERNS))
def test_no_secret_value_is_tracked(label):
    """Тестовые заготовки исключены: они выдуманы и никуда не ведут."""
    pattern = SECRET_PATTERNS[label]
    hits = []
    for path in TRACKED:
        if path in TEST_FILES or not path.is_file():
            continue
        if path.suffix in {".png", ".jpg", ".ico", ".woff", ".woff2", ".gz", ".zip"}:
            continue
        try:
            text = path.read_text(encoding="utf-8-sig", errors="strict")
        except (UnicodeDecodeError, OSError):
            continue
        for line in text.splitlines():
            if not pattern.search(line):
                continue
            if looks_like_a_placeholder(line):
                continue
            hits.append(f"{path.relative_to(ROOT)}: {line.strip()[:60]}")
    assert not hits, f"похоже на «{label}» в: {hits}"


def test_the_public_key_is_never_shipped_inside_the_repository():
    """Ключ привозят отдельным файлом и сверяют по отпечатку.

    Если бы он лежал в репозитории, закрепление ключа перестало бы быть
    отдельным решением администратора.
    """
    hits = [
        str(p.relative_to(ROOT))
        for p in TRACKED
        if p.suffix == ".pem" or "ed25519" in p.name
    ]
    assert not hits, f"ключ приехал бы вместе с кодом: {hits}"


def test_the_emergency_scripts_are_all_tracked():
    """Иначе на станцию приедет не весь инструментарий."""
    names = {p.name for p in TRACKED if p.suffix == ".ps1"}
    for required in (
        "Install-DenisStock-EmergencyWorkstation.ps1",
        "Test-DenisStockEmergencyPreflight.ps1",
        "Test-DenisStockEmergency.ps1",
        "Collect-DenisStockEmergencyDiagnostics.ps1",
        "Protect-DenisStockEmergencyCredentials.ps1",
        "DenisStock-Emergency.ps1",
        "Emergency-Standby-Refresh.ps1",
        "EmergencyBackupSource.ps1",
    ):
        assert required in names, f"{required} не отслеживается и не приедет на станцию"
