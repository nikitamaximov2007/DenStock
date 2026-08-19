"""Версия приложения не должна теряться при развёртывании молча.

На неё опираются manifest резервной копии, активация аварийного режима и сверка
при возврате на production. Настройка необязательна и по умолчанию пуста, а
запасной вариант читает git, которого в контейнере может не быть.

Это уже приводило к копиям без метаданных, поэтому проверка живёт в `ops_check`,
а не в документе: развёртывание можно проверить командой, а инструкцию нельзя.
"""
from __future__ import annotations

import pytest

from apps.operations import checks


def _result(results, name):
    found = [r for r in results if r.name == name]
    assert found, f"проверка «{name}» отсутствует в ops_check"
    return found[0]


def test_ops_check_reports_the_configured_app_version(db, settings):
    settings.DENSTOCK_APP_COMMIT = "a" * 40
    result = _result(checks.run_checks(), "Версия приложения")
    assert result.level == checks.OK
    assert "aaaaaaaaaaaa" in result.message


def test_ops_check_fails_when_version_is_missing_everywhere(db, settings, monkeypatch):
    """Контейнер без .git и без переменной: это авария развёртывания."""
    settings.DENSTOCK_APP_COMMIT = ""
    monkeypatch.setattr(checks.backup, "_git_commit", lambda: None)

    results = checks.run_checks()
    result = _result(results, "Версия приложения")

    assert result.level == checks.FAIL
    assert checks.has_failures(results), "ops_check не считает это отказом"


def test_ops_check_warns_when_version_only_comes_from_git(db, settings, monkeypatch):
    """Локально git есть, в контейнере его не будет: это предупреждение."""
    settings.DENSTOCK_APP_COMMIT = ""
    monkeypatch.setattr(checks.backup, "_git_commit", lambda: "b" * 40)

    result = _result(checks.run_checks(), "Версия приложения")

    assert result.level == checks.WARN
    assert "git" in result.message


def test_blank_setting_is_treated_as_missing(db, settings, monkeypatch):
    """Пробелы в переменной окружения не считаются заданной версией."""
    settings.DENSTOCK_APP_COMMIT = "   "
    monkeypatch.setattr(checks.backup, "_git_commit", lambda: None)
    assert _result(checks.run_checks(), "Версия приложения").level == checks.FAIL


@pytest.mark.parametrize(
    "name",
    ["База данных", "MEDIA_ROOT", "BACKUP_ROOT", "DEBUG", "SECRET_KEY", "Версия приложения"],
)
def test_ops_check_still_covers_every_operational_area(db, name):
    """Новая проверка не должна вытеснить существующие."""
    _result(checks.run_checks(), name)
