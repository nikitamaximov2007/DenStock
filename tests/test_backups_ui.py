"""v1.1.7 — Backup UI (owner/admin only, read-only + создание локального бэкапа).

Web-restore специально отсутствует. Скачивание — только разрешённые файлы из backup-run
(защита от path traversal). Складская логика не затрагивается.
"""
import json
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.urls import NoReverseMatch, reverse

from apps.accounts import roles
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.operations import backup as backup_mod
from apps.procurement.models import Batch, BatchLine

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def backups_root(settings, tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    settings.BACKUP_ROOT = root
    return root


def _make_run(root, run_id="2026-06-30_12-00-00", manifest=True, files=("db.dump", "media.tar.gz")):
    run = Path(root) / run_id
    run.mkdir(parents=True, exist_ok=True)
    for name in files:
        (run / name).write_bytes(b"backup-bytes")
    if manifest:
        (run / "manifest.json").write_text(
            json.dumps({
                "created_at": "2026-06-30T12:00:00", "engine": "sqlite",
                "db_file": "db.dump", "media_file": "media.tar.gz",
                "version": "0.1.0", "git_commit": "abc1234",
            }),
            encoding="utf-8",
        )
    return run


def _admin(make_user, client):
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)


# --- Доступ ------------------------------------------------------------------


def test_admin_sees_backups(make_user, client, backups_root):
    _admin(make_user, client)
    resp = client.get(reverse("operations:backups"))
    assert resp.status_code == 200
    assert "Локальные бэкапы" in resp.content.decode()


@pytest.mark.parametrize("role", [roles.SELLER, roles.STOREKEEPER, roles.MANAGER, roles.VIEWER])
def test_non_admin_forbidden(make_user, client, backups_root, role):
    make_user("u", role=role)
    client.login(username="u", password=PASSWORD)
    assert client.get(reverse("operations:backups")).status_code == 403


def test_anonymous_redirected(client, backups_root):
    assert client.get(reverse("operations:backups")).status_code == 302


def test_nav_item_only_for_admin(make_user, client, backups_root):
    _admin(make_user, client)
    assert "Бэкапы" in client.get(reverse("dashboard")).content.decode()
    client.logout()
    make_user("seller", role=roles.SELLER)
    client.login(username="seller", password=PASSWORD)
    assert "Бэкапы" not in client.get(reverse("dashboard")).content.decode()


# --- Список / manifest -------------------------------------------------------


def test_list_shows_runs(make_user, client, backups_root):
    _make_run(backups_root)
    _admin(make_user, client)
    html = client.get(reverse("operations:backups")).content.decode()
    assert "2026-06-30_12-00-00" in html
    assert "db.dump" in html
    assert "media.tar.gz" in html


def test_manifest_view_shows_fields(make_user, client, backups_root):
    _make_run(backups_root)
    _admin(make_user, client)
    html = client.get(
        reverse("operations:backup_manifest", args=["2026-06-30_12-00-00"])
    ).content.decode()
    assert "sqlite" in html
    assert "abc1234" in html


def test_broken_manifest_handled(make_user, client, backups_root):
    run = _make_run(backups_root, run_id="2026-06-29_00-00-00", manifest=False)
    (run / "manifest.json").write_text("{ broken json", encoding="utf-8")
    _admin(make_user, client)
    resp = client.get(reverse("operations:backup_manifest", args=["2026-06-29_00-00-00"]))
    assert resp.status_code == 200  # не 500
    assert "повреждён" in resp.content.decode()


def test_missing_manifest_handled(make_user, client, backups_root):
    _make_run(backups_root, run_id="2026-06-28_00-00-00", manifest=False)
    _admin(make_user, client)
    resp = client.get(reverse("operations:backup_manifest", args=["2026-06-28_00-00-00"]))
    assert resp.status_code == 200
    assert "отсутствует" in resp.content.decode()


# --- Создание бэкапа ---------------------------------------------------------


def test_create_calls_backup_all(make_user, client, backups_root, monkeypatch):
    called = {"backup": False, "restore": False}

    def fake_backup_all():
        called["backup"] = True
        return _make_run(backups_root, run_id="2026-07-01_00-00-00")

    def boom(*a, **k):
        called["restore"] = True
        raise AssertionError("restore из web запрещён")

    monkeypatch.setattr(backup_mod, "backup_all", fake_backup_all)
    monkeypatch.setattr(backup_mod, "restore_db", boom)
    monkeypatch.setattr(backup_mod, "restore_media", boom)

    _admin(make_user, client)
    resp = client.post(reverse("operations:backup_create"))
    assert resp.status_code == 302  # redirect на список
    assert called["backup"] is True
    assert called["restore"] is False


def test_create_is_post_only(make_user, client, backups_root):
    _admin(make_user, client)
    assert client.get(reverse("operations:backup_create")).status_code == 405


def test_create_forbidden_for_non_admin(make_user, client, backups_root):
    make_user("seller", role=roles.SELLER)
    client.login(username="seller", password=PASSWORD)
    assert client.post(reverse("operations:backup_create")).status_code == 403


# --- Скачивание / path traversal ---------------------------------------------


def test_download_allowed_file(make_user, client, backups_root):
    _make_run(backups_root)
    _admin(make_user, client)
    resp = client.get(
        reverse("operations:backup_download", args=["2026-06-30_12-00-00", "db.dump"])
    )
    assert resp.status_code == 200


def test_download_rejects_disallowed_filename(make_user, client, backups_root):
    run = _make_run(backups_root)
    (run / "secret.txt").write_text("nope", encoding="utf-8")
    _admin(make_user, client)
    resp = client.get(
        reverse("operations:backup_download", args=["2026-06-30_12-00-00", "secret.txt"])
    )
    assert resp.status_code == 404


def test_download_missing_run(make_user, client, backups_root):
    _admin(make_user, client)
    resp = client.get(
        reverse("operations:backup_download", args=["nonexistent", "db.dump"])
    )
    assert resp.status_code == 404


def test_manifest_missing_run(make_user, client, backups_root):
    _admin(make_user, client)
    assert client.get(
        reverse("operations:backup_manifest", args=["nonexistent"])
    ).status_code == 404


# --- Offsite / отсутствие web-restore ----------------------------------------


def test_offsite_not_configured(make_user, client, backups_root):
    _admin(make_user, client)
    assert "не настроено" in client.get(reverse("operations:backups")).content.decode()


def test_no_web_restore_url():
    for name in ("operations:restore_db", "operations:restore_media", "operations:restore"):
        with pytest.raises(NoReverseMatch):
            reverse(name)


# --- Read-only относительно склада -------------------------------------------


def test_backup_ui_does_not_touch_stock(make_user, client, backups_root, monkeypatch):
    monkeypatch.setattr(backup_mod, "backup_all", lambda: _make_run(backups_root, "r1"))
    _admin(make_user, client)
    mv = StockMovement.objects.count()
    bal = StockBalance.objects.count()
    client.get(reverse("operations:backups"))
    client.post(reverse("operations:backup_create"))
    assert StockMovement.objects.count() == mv
    assert StockBalance.objects.count() == bal
    assert StockLot.objects.count() == 0
    assert PartItem.objects.count() == 0
    assert Batch.objects.count() == 0
    assert BatchLine.objects.count() == 0


def test_backups_gitignored():
    text = (Path(backup_mod.settings.BASE_DIR) / ".gitignore").read_text(encoding="utf-8")
    assert "/backups/" in text
