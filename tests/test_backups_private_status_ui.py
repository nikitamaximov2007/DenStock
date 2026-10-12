"""The backups list tells the operator which copies carry private files."""

import json

import pytest
from django.db import connection
from django.urls import reverse

from apps.operations import backup

pytest_plugins = ("tests.test_private_media_backup",)

sqlite_only = pytest.mark.skipif(
    connection.vendor != "sqlite",
    reason="fixture backups are SQLite; the engine guard rejects them on PostgreSQL",
)


def _manifest(run):
    return json.loads((run / "manifest.json").read_text(encoding="utf-8"))


def _write_manifest(run, manifest):
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _tick(monkeypatch):
    names = iter(f"2026-10-12_03-00-{second:02d}" for second in range(60))
    monkeypatch.setattr(backup, "timestamp", lambda: next(names))


@sqlite_only
def test_backups_list_marks_copies_without_private_files(
    production, settings, client, django_user_model, monkeypatch
):
    _tick(monkeypatch)
    legacy = production["make"]()
    manifest = _manifest(legacy)
    for key in [k for k in manifest if k.startswith("private_media")]:
        manifest.pop(key)
    _write_manifest(legacy, manifest)
    (legacy / "private_media.tar.gz").unlink()
    current = production["make"]()
    settings.DENSTOCK_ENABLE_WEB_RESTORE = True
    settings.DENSTOCK_RESTORE_ALLOWED_USERNAMES = ["owner"]
    settings.DENSTOCK_RESTORE_ALLOWED_EMAILS = []
    owner = django_user_model.objects.create_superuser("owner", "o@example.com", "x")
    client.force_login(owner)

    html = client.get(reverse("operations:backups")).content.decode()

    assert html.count("без приватных файлов") == 1
    legacy_row = html[html.index(legacy.name):html.index(legacy.name) + 400]
    assert "без приватных файлов" in legacy_row
    assert current.name in html
    assert "<th>Приватные</th>" in html
    assert "—" not in html[html.index("Аварийное восстановление"):]
