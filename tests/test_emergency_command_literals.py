"""Буквальность команд, которые завтра набирают у компьютера.

Опечатка в строке источника копий или устаревший адрес не ловятся ни одним
обычным тестом: сценарии всё равно проходят, а команда из документа не
работает. Поэтому буквы проверяются отдельно.

Главный случай: в PowerShell двоеточие обратной косой не экранируется. Строка
вида yandex-s3\:denstock-backups-nikita до rclone дойдёт неправильной, и
человек будет искать причину в облаке, а не в документе.
"""
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
SCRIPTS = ROOT / "scripts"

BACKSLASH = chr(92)
CORRECT_SOURCE = "yandex-s3:denstock-backups-nikita"
WRONG_SOURCE = "yandex-s3" + BACKSLASH + ":"

OLD_PRODUCTION_IP = "91.142.73.205"
CURRENT_PRODUCTION_IP = "185.250.44.206"

EMERGENCY_DOCS = sorted(DOCS.glob("operations/emergency-*.md"))


def readable_files():
    for base in (DOCS, SCRIPTS):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".md", ".ps1", ".sh"}:
                yield path


def test_there_are_emergency_docs_to_check():
    assert EMERGENCY_DOCS, "документы аварийного режима не найдены"


@pytest.mark.parametrize("path", list(readable_files()), ids=lambda p: p.name)
def test_the_backup_source_colon_is_never_escaped(path):
    """В PowerShell двоеточие так не экранируется: строка дойдёт испорченной."""
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    assert WRONG_SOURCE not in text, (
        f"{path.name}: источник копий записан с обратной косой перед двоеточием"
    )


def test_the_backup_source_is_written_the_same_way_everywhere():
    """Расхождение написания означает, что где-то команда не сработает."""
    seen = set()
    for path in readable_files():
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        for line in text.splitlines():
            if "denstock-backups-nikita" in line:
                start = line.index("denstock-backups-nikita")
                prefix = line[max(0, start - 12):start]
                if "yandex-s3" in prefix:
                    seen.add(prefix[prefix.index("yandex-s3"):])
    assert seen, "проверка бессмысленна: строка источника нигде не встречается"
    assert seen == {"yandex-s3:"}, f"источник записан по-разному: {sorted(seen)}"


@pytest.mark.parametrize("path", EMERGENCY_DOCS, ids=lambda p: p.name)
def test_emergency_docs_never_point_at_the_old_production_address(path):
    """Старый адрес не должен попасть в команду, которую наберут у компьютера."""
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    assert OLD_PRODUCTION_IP not in text, f"{path.name}: старый адрес production"


def test_operational_docs_do_not_teach_the_old_address():
    """Он остаётся допустимым в списке разрешённых хостов, но не в примерах."""
    for path in DOCS.glob("operations/*.md"):
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
        for number, line in enumerate(text.splitlines(), start=1):
            if OLD_PRODUCTION_IP not in line:
                continue
            pytest.fail(f"{path.name}:{number} учит старому адресу: {line.strip()}")


def test_emergency_docs_use_the_current_production_address():
    kit = (DOCS / "operations" / "emergency-install-kit.md").read_text(encoding="utf-8")
    checklist = (
        DOCS / "operations" / "emergency-physical-install-checklist.md"
    ).read_text(encoding="utf-8")
    for text, name in ((kit, "инструкция"), (checklist, "лист выполнения")):
        assert CURRENT_PRODUCTION_IP in text or "sslip.io" in text, f"{name}: нет адреса production"


@pytest.mark.parametrize("path", list(readable_files()), ids=lambda p: p.name)
def test_no_stale_branch_reference(path):
    """Ветка, которой не было на сервере, однажды уже сорвала выкладку."""
    text = path.read_text(encoding="utf-8-sig", errors="ignore")
    assert "prepared/operator-ux" not in text, f"{path.name}: ссылка на несуществующую ветку"
