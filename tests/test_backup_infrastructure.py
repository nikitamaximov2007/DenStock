"""Ночной бэкап production обязан восстанавливаться из Git.

До этого скрипт и unit-файлы жили только на сервере: в репозитории их не было
ни в одной ветке. Потеря машины означала бы потерю самого механизма, который
делает подписанные копии, а именно на них держится восстановление.

Здесь закреплено то, что нельзя потерять при правках: файлы отслеживаются по
каноническим путям, unit указывает на установленный скрипт, расписание то же,
что на работающем сервере, секретов внутри нет, а окончания строк остаются
LF, иначе bash не прочитает shebang.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

SCRIPT = ROOT / "deploy" / "backup" / "bin" / "denstock-backup-capped"
SERVICE = ROOT / "deploy" / "backup" / "systemd" / "denstock-backup.service"
TIMER = ROOT / "deploy" / "backup" / "systemd" / "denstock-backup.timer"

# Куда файлы ставятся на сервере. Unit запускается от root и не должен зависеть
# от того, какая версия кода сейчас выкачана в /opt/denstock.
INSTALLED_SCRIPT = "/usr/local/sbin/denstock-backup-capped"


def tracked(pattern):
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", pattern],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert output.returncode == 0, output.stderr
    return [line for line in output.stdout.split("\n") if line.strip()]


def unit_values(path):
    """Разбор unit-файла: systemd допускает повторяющиеся ключи, поэтому список."""
    values = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";", "[")):
            continue
        key, _, value = line.partition("=")
        values.setdefault(key.strip(), []).append(value.strip())
    return values


def sections(path):
    return re.findall(r"^\[([^\]]+)\]$", path.read_text(encoding="utf-8"), re.MULTILINE)


@pytest.mark.parametrize("path", [SCRIPT, SERVICE, TIMER], ids=lambda p: p.name)
def test_backup_infrastructure_is_tracked(path):
    assert path.is_file(), f"{path.name} отсутствует в репозитории"
    assert tracked(str(path.relative_to(ROOT)).replace("\\", "/")), (
        f"{path.name} лежит на диске, но не отслеживается Git"
    )


def test_backup_script_is_executable_in_git():
    """Права хранит сам Git: после clone скрипт обязан остаться исполняемым."""
    output = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-s", "deploy/backup/bin/denstock-backup-capped"],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    mode = output.stdout.split()[0] if output.stdout else "отсутствует"
    assert mode == "100755", f"скрипт должен быть 100755, а не {mode}"


@pytest.mark.parametrize("path", [SCRIPT, SERVICE, TIMER], ids=lambda p: p.name)
def test_linux_files_keep_lf_endings(path):
    """CR в shebang превращает запуск в 'bad interpreter'."""
    assert b"\r" not in path.read_bytes(), (
        f"{path.name}: появились CRLF, Linux этого не простит"
    )


@pytest.mark.parametrize("path", [SCRIPT, SERVICE, TIMER], ids=lambda p: p.name)
def test_linux_files_are_pinned_to_lf_by_gitattributes(path):
    """Без правила Windows-клон получит CRLF и сломает файл при установке."""
    relative = str(path.relative_to(ROOT)).replace("\\", "/")
    output = subprocess.run(
        ["git", "-C", str(ROOT), "check-attr", "eol", "--", relative],
        capture_output=True, text=True, encoding="utf-8", timeout=120,
    )
    assert output.stdout.strip().endswith("eol: lf"), (
        f"{relative}: нет правила eol=lf в .gitattributes ({output.stdout.strip()})"
    )


def test_service_runs_the_installed_script_and_needs_its_configuration():
    values = unit_values(SERVICE)
    assert sections(SERVICE) == ["Unit", "Service"]
    assert values["ExecStart"] == [INSTALLED_SCRIPT]
    assert Path(INSTALLED_SCRIPT).name == SCRIPT.name, (
        "имя установленного скрипта разошлось с отслеживаемым"
    )
    # Без конфигурации и без самого скрипта служба не должна стартовать.
    assert "/opt/denstock/.env.backup" in values["ConditionPathExists"]
    assert INSTALLED_SCRIPT in values["ConditionPathExists"]
    assert values["Type"] == ["oneshot"]
    assert values["WorkingDirectory"] == ["/opt/denstock"]
    # Бэкап не должен конкурировать за диск со складом в рабочее время.
    assert values["Nice"] == ["10"]


def test_timer_keeps_the_live_schedule():
    values = unit_values(TIMER)
    assert sections(TIMER) == ["Unit", "Timer", "Install"]
    assert values["OnCalendar"] == ["*-*-* 03:00:00 Europe/Moscow"]
    # Выключенный ночью сервер не должен пропустить сутки молча.
    assert values["Persistent"] == ["true"]
    assert values["Unit"] == ["denstock-backup.service"]
    assert values["WantedBy"] == ["timers.target"]


def test_script_keeps_the_operational_guarantees_it_had_live():
    text = SCRIPT.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    # Падать, а не продолжать с половиной результата.
    assert "set -euo pipefail" in text
    # Два бэкапа одновременно не идут.
    assert "/run/lock/denstock-backup.lock" in text
    assert "flock" in text
    # Копию делает штатная команда приложения, она же её подписывает.
    # Вызов разнесён по строкам продолжениями, поэтому части проверяются отдельно.
    assert "manage.py backup_all" in text
    assert "--trigger automatic" in text
    assert '--keep-last "$BACKUP_KEEP_LAST"' in text
    # На версионированном бакете удалённые объекты продолжают занимать место,
    # поэтому размер спрашивается с версиями, а purge дополняется cleanup-hidden.
    assert "rclone size --s3-versions" in text
    assert "cleanup-hidden" in text
    assert "BACKUP_REMOTE_SOFT_LIMIT_BYTES" in text


SECRET_PATTERNS = {
    "приватный ключ": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "ключ доступа AWS": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "статический ключ Yandex": re.compile(r"\bYC[A-Za-z0-9_-]{30,}\b"),
    "присвоенный секрет": re.compile(
        r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*=\s*[\"']?[A-Za-z0-9/+_-]{12,}"
    ),
}


@pytest.mark.parametrize("label", sorted(SECRET_PATTERNS))
@pytest.mark.parametrize("path", [SCRIPT, SERVICE, TIMER], ids=lambda p: p.name)
def test_no_secret_value_lives_in_backup_infrastructure(path, label):
    text = path.read_text(encoding="utf-8")
    assert not SECRET_PATTERNS[label].search(text), (
        f"{path.name}: похоже на {label}; значения должны оставаться в .env.backup"
    )


def test_backup_configuration_itself_is_not_tracked():
    assert not tracked(".env.backup"), "конфигурация бэкапа не должна попадать в Git"
    assert tracked(".env.backup.example"), "шаблон конфигурации обязан быть в Git"


def test_example_documents_every_variable_the_script_reads():
    """Иначе новый сервер не узнает про существующую настройку."""
    script = SCRIPT.read_text(encoding="utf-8")
    example = (ROOT / ".env.backup.example").read_text(encoding="utf-8")
    used = set(re.findall(r"\$\{?(BACKUP_[A-Z_]+)", script))
    assert used, "в скрипте не нашлось ни одной переменной BACKUP_*"
    missing = sorted(name for name in used if name not in example)
    assert not missing, f"не описаны в .env.backup.example: {missing}"


def test_install_instructions_exist_and_do_not_start_a_backup():
    readme = (ROOT / "deploy" / "backup" / "README.md").read_text(encoding="utf-8")
    assert INSTALLED_SCRIPT in readme
    assert "/etc/systemd/system/denstock-backup.service" in readme
    assert "systemctl daemon-reload" in readme
    # Включается расписание, а не сам бэкап: запуск копии остаётся осознанным.
    assert "systemctl enable --now denstock-backup.timer" in readme
    assert "systemctl start denstock-backup.service" in readme
    where = readme.index("systemctl start denstock-backup.service")
    around = readme[where - 400:where + 400]
    assert "вручную" in around or "осознанно" in around, (
        "немедленный прогон должен быть описан как отдельное осознанное действие"
    )
