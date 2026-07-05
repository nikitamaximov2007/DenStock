"""Layer 30 — защищённое аварийное восстановление из веб-интерфейса.

Гарантии: restore видит ТОЛЬКО allowlist-владелец при включённом флаге;
GET ничего не запускает; без фразы ПОДТВЕРЖДАЮ и checkbox restore не
стартует; verify обязателен; pre-restore бэкап создаётся ДО restore, а его
ошибка отменяет restore целиком; path traversal невозможен; обычный
экспорт бэкапов не сломан (существующие тесты test_backups_ui).
"""
import json
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.operations import backup as backup_mod
from apps.operations import restore as restore_mod
from apps.operations.models import RestoreJob
from apps.operations.restore import CONFIRM_PHRASE, run_web_restore, verify_backup

PASSWORD = "parol-12345"
OWNER_EMAIL = "nikita.maximov2007@gmail.com"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False, email=""):
        if is_superuser:
            user = django_user_model.objects.create_superuser(
                username=username, password=PASSWORD, email=email
            )
        else:
            user = django_user_model.objects.create_user(
                username=username, password=PASSWORD, email=email
            )
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


@pytest.fixture
def restore_enabled(settings):
    settings.DENSTOCK_ENABLE_WEB_RESTORE = True
    settings.DENSTOCK_RESTORE_ALLOWED_EMAILS = [OWNER_EMAIL]
    settings.DENSTOCK_RESTORE_ALLOWED_USERNAMES = []
    return settings


def _make_run(root, run_id="2026-07-01_10-00-00", manifest=True,
              files=("db.dump", "media.tar.gz"), engine="sqlite", **manifest_extra):
    run = Path(root) / run_id
    run.mkdir(parents=True, exist_ok=True)
    for name in files:
        (run / name).write_bytes(b"backup-bytes")
    if manifest:
        data = {
            "created_at": "2026-07-01T10:00:00", "type": "manual", "engine": engine,
            "db_file": "db.dump" if "db.dump" in files else None,
            "media_file": "media.tar.gz" if "media.tar.gz" in files else None,
            "version": None, "git_commit": "abc1234",
        }
        data.update(manifest_extra)
        (run / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
    return run


def _login_owner(client, make_user):
    user = make_user("owner", is_superuser=True, email=OWNER_EMAIL)
    client.login(username="owner", password=PASSWORD)
    return user


RESTORE_BLOCK = "Аварийное восстановление"


# --- Кто видит restore UI -----------------------------------------------------


def test_plain_admin_does_not_see_restore(client, make_user, backups_root, restore_enabled):
    """Обычный админ без allowlist: раздел бэкапов есть, restore-блока нет."""
    make_user("boss", is_superuser=True)  # email пустой -> не в allowlist
    client.login(username="boss", password=PASSWORD)
    _make_run(backups_root)
    html = client.get(reverse("operations:backups")).content.decode()
    assert RESTORE_BLOCK not in html
    resp = client.get(reverse("operations:backup_restore", args=["2026-07-01_10-00-00"]))
    assert resp.status_code == 403


@pytest.mark.parametrize("role", [roles.MANAGER, roles.STOREKEEPER, roles.SELLER, roles.VIEWER])
def test_non_admin_roles_blocked(client, make_user, backups_root, restore_enabled, role):
    make_user("u", role=role, email=OWNER_EMAIL)  # даже с email владельца: не админ
    client.login(username="u", password=PASSWORD)
    _make_run(backups_root)
    assert client.get(reverse("operations:backups")).status_code == 403
    resp = client.get(reverse("operations:backup_restore", args=["2026-07-01_10-00-00"]))
    assert resp.status_code == 403


def test_allowlisted_owner_sees_restore(client, make_user, backups_root, restore_enabled):
    _login_owner(client, make_user)
    _make_run(backups_root)
    html = client.get(reverse("operations:backups")).content.decode()
    assert RESTORE_BLOCK in html
    assert "Проверить бэкап" in html


def test_flag_off_hides_restore_even_for_owner(client, make_user, backups_root, settings):
    settings.DENSTOCK_ENABLE_WEB_RESTORE = False
    _login_owner(client, make_user)
    _make_run(backups_root)
    html = client.get(reverse("operations:backups")).content.decode()
    assert RESTORE_BLOCK not in html
    resp = client.get(reverse("operations:backup_restore", args=["2026-07-01_10-00-00"]))
    assert resp.status_code == 403


def test_username_allowlist_works(client, make_user, backups_root, restore_enabled):
    restore_enabled.DENSTOCK_RESTORE_ALLOWED_EMAILS = []
    restore_enabled.DENSTOCK_RESTORE_ALLOWED_USERNAMES = ["boss"]
    make_user("boss", is_superuser=True)
    client.login(username="boss", password=PASSWORD)
    _make_run(backups_root)
    assert RESTORE_BLOCK in client.get(reverse("operations:backups")).content.decode()


# --- Verify ---------------------------------------------------------------------


def test_valid_backup_passes_verify(backups_root):
    _make_run(backups_root)
    report = verify_backup("2026-07-01_10-00-00")
    assert report.ok
    assert report.db_file == "db.dump"
    assert report.media_file == "media.tar.gz"


def test_missing_manifest_rejected(backups_root):
    _make_run(backups_root, manifest=False)
    report = verify_backup("2026-07-01_10-00-00")
    assert not report.ok


def test_missing_db_rejected(backups_root):
    _make_run(backups_root, files=("media.tar.gz",))
    report = verify_backup("2026-07-01_10-00-00")
    assert not report.ok


def test_engine_mismatch_rejected(backups_root):
    _make_run(backups_root, engine="postgresql")  # тестовая БД sqlite
    report = verify_backup("2026-07-01_10-00-00")
    assert not report.ok


def test_path_traversal_rejected(backups_root, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "manifest.json").write_text("{}", encoding="utf-8")
    for bad in ("..", "../outside", "..\\outside", "a/../b", ""):
        report = verify_backup(bad)
        assert not report.ok, bad


def test_old_backup_and_pre_restore_warnings(backups_root):
    _make_run(backups_root, created_at="2020-01-01T00:00:00", type="pre_restore")
    report = verify_backup("2026-07-01_10-00-00")
    assert report.ok  # предупреждения не блокируют
    joined = " ".join(report.warnings)
    assert "дней" in joined and "pre-restore" in joined


def test_verify_command(backups_root, capsys):
    from django.core.management import CommandError, call_command
    _make_run(backups_root)
    call_command("verify_backup", "2026-07-01_10-00-00")
    _make_run(backups_root, run_id="bad", manifest=False)
    with pytest.raises(CommandError):
        call_command("verify_backup", "bad")


# --- Подтверждение (view) ---------------------------------------------------------


@pytest.fixture
def owner_client(client, make_user, backups_root, restore_enabled):
    _login_owner(client, make_user)
    _make_run(backups_root)
    return client


def _boom(*args, **kwargs):
    raise AssertionError("restore не должен был запуститься")


def test_get_never_runs_restore(owner_client, monkeypatch):
    monkeypatch.setattr(restore_mod, "run_web_restore", _boom)
    url = reverse("operations:backup_restore", args=["2026-07-01_10-00-00"])
    resp = owner_client.get(url)
    assert resp.status_code == 200
    html = resp.content.decode()
    assert CONFIRM_PHRASE in html
    assert 'type="file"' not in html.lower()  # upload бэкапов не реализован
    assert RestoreJob.objects.count() == 0


def test_post_without_phrase_rejected(owner_client, monkeypatch):
    monkeypatch.setattr(restore_mod, "run_web_restore", _boom)
    url = reverse("operations:backup_restore", args=["2026-07-01_10-00-00"])
    resp = owner_client.post(url, {"accept_risk": "on"})
    assert resp.status_code == 200
    assert "Фраза подтверждения неверна" in resp.content.decode()


def test_post_wrong_phrase_rejected(owner_client, monkeypatch):
    monkeypatch.setattr(restore_mod, "run_web_restore", _boom)
    url = reverse("operations:backup_restore", args=["2026-07-01_10-00-00"])
    resp = owner_client.post(url, {"confirm_phrase": "подтверждаю", "accept_risk": "on"})
    assert "Фраза подтверждения неверна" in resp.content.decode()


def test_post_without_checkbox_rejected(owner_client, monkeypatch):
    monkeypatch.setattr(restore_mod, "run_web_restore", _boom)
    url = reverse("operations:backup_restore", args=["2026-07-01_10-00-00"])
    resp = owner_client.post(url, {"confirm_phrase": CONFIRM_PHRASE})
    assert "будут перезаписаны" in resp.content.decode()


def test_post_on_unverified_backup_rejected(owner_client, backups_root, monkeypatch):
    monkeypatch.setattr(restore_mod, "run_web_restore", _boom)
    _make_run(backups_root, run_id="broken", manifest=False)
    url = reverse("operations:backup_restore", args=["broken"])
    resp = owner_client.post(url, {"confirm_phrase": CONFIRM_PHRASE, "accept_risk": "on"})
    assert "не прошёл проверку" in resp.content.decode()


def test_post_with_phrase_and_checkbox_runs(owner_client, monkeypatch):
    calls = []

    def fake_run(run_id, *, user):
        calls.append(run_id)
        return RestoreJob.objects.create(
            run_id=run_id, status=RestoreJob.Status.COMPLETED,
            started_by_username=user.username, pre_restore_run_id="pre-1",
            log="ok",
        )

    monkeypatch.setattr(restore_mod, "run_web_restore", fake_run)
    url = reverse("operations:backup_restore", args=["2026-07-01_10-00-00"])
    resp = owner_client.post(url, {"confirm_phrase": CONFIRM_PHRASE, "accept_risk": "on"})
    assert calls == ["2026-07-01_10-00-00"]
    html = resp.content.decode()
    assert "Восстановление завершено" in html
    assert "pre-1" in html


def test_nonexistent_backup_404(owner_client):
    resp = owner_client.get(reverse("operations:backup_restore", args=["no-such-run"]))
    assert resp.status_code == 404


# --- Безопасность выполнения (сервис) ------------------------------------------------


def test_pre_restore_backup_called_before_restore(backups_root, make_user, monkeypatch):
    user = make_user("owner", is_superuser=True, email=OWNER_EMAIL)
    _make_run(backups_root)
    order = []
    pre_dir = _make_run(backups_root, run_id="2026-07-02_09-00-00", type="pre_restore")
    monkeypatch.setattr(
        backup_mod, "backup_all",
        lambda **kw: order.append("pre_backup") or pre_dir,
    )
    monkeypatch.setattr(restore_mod.backup, "restore_db", lambda p: order.append("db"))
    monkeypatch.setattr(restore_mod.backup, "restore_media", lambda p: order.append("media"))
    monkeypatch.setattr(restore_mod, "call_command", lambda *a, **kw: order.append("migrate"))

    job = run_web_restore("2026-07-01_10-00-00", user=user)
    assert order == ["pre_backup", "db", "media", "migrate"]
    assert job.status == RestoreJob.Status.COMPLETED
    assert job.pre_restore_run_id == "2026-07-02_09-00-00"
    assert job.started_by_username == "owner"
    assert (backups_root / "restore.log").exists()  # файловый след


def test_failed_pre_backup_cancels_restore(backups_root, make_user, monkeypatch):
    user = make_user("owner", is_superuser=True, email=OWNER_EMAIL)
    _make_run(backups_root)

    def failing_backup(**kwargs):
        raise backup_mod.OperationsError("нет места на диске")

    monkeypatch.setattr(backup_mod, "backup_all", failing_backup)
    monkeypatch.setattr(restore_mod.backup, "restore_db", _boom)
    monkeypatch.setattr(restore_mod.backup, "restore_media", _boom)

    job = run_web_restore("2026-07-01_10-00-00", user=user)
    assert job.status == RestoreJob.Status.FAILED
    assert "Pre-restore бэкап не создан" in job.error


def test_failed_verify_skips_pre_backup(backups_root, make_user, monkeypatch):
    user = make_user("owner", is_superuser=True, email=OWNER_EMAIL)
    _make_run(backups_root, run_id="broken", manifest=False)
    monkeypatch.setattr(backup_mod, "backup_all", _boom)
    job = run_web_restore("broken", user=user)
    assert job.status == RestoreJob.Status.FAILED
    assert "Проверка бэкапа" in job.error


def test_restore_error_reports_pre_backup_for_rollback(backups_root, make_user, monkeypatch):
    user = make_user("owner", is_superuser=True, email=OWNER_EMAIL)
    _make_run(backups_root)
    pre_dir = _make_run(backups_root, run_id="2026-07-02_09-00-00", type="pre_restore")
    monkeypatch.setattr(backup_mod, "backup_all", lambda **kw: pre_dir)

    def failing_restore(path):
        raise backup_mod.OperationsError("pg_restore упал")

    monkeypatch.setattr(restore_mod.backup, "restore_db", failing_restore)
    job = run_web_restore("2026-07-01_10-00-00", user=user)
    assert job.status == RestoreJob.Status.FAILED
    assert "2026-07-02_09-00-00" in job.error  # путь отката виден


# --- Гигиена -------------------------------------------------------------------------


def test_restore_pages_have_no_em_dash(owner_client):
    for url in (
        reverse("operations:backups"),
        reverse("operations:backup_restore", args=["2026-07-01_10-00-00"]),
    ):
        assert "—" not in owner_client.get(url).content.decode()
