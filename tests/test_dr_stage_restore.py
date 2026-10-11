"""Offline DR staging restore: new database + new directories + durable journal.

Guards run everywhere; the restore itself needs an isolated PostgreSQL 16.
"""

import json
import shutil
import uuid

import psycopg
import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import connection
from django.db.backends.base.base import BaseDatabaseWrapper

from apps.catalog.models import PartTypeImage
from apps.operations import backup, offline_restore
from apps.operations.emergency_manifest import PRIVATE_FIELDS
from apps.operations.offline_restore import (
    ExternalPrivateMedia,
    StageRestoreError,
    StageTargets,
    discard_stage,
    read_journal,
    stage_restore,
)
from apps.operations.private_media import Inventory
from tests.emergency_support import configure_test_trust, sign_production_manifest
from tests.test_telegram_customer_messaging import build_part

PG = {"ENGINE": "django.db.backends.postgresql", "NAME": "denstock_live"}


def _targets(tmp_path, **overrides):
    values = {
        "database": "dr_stage_test",
        "media_dir": tmp_path / "stage-media",
        "private_media_dir": tmp_path / "stage-private",
        "journal": tmp_path / "stage.journal",
    }
    values.update(overrides)
    return StageTargets(**values)


@pytest.fixture
def live_roots(tmp_path, settings):
    settings.MEDIA_ROOT = tmp_path / "live-media"
    settings.PRIVATE_MEDIA_ROOT = tmp_path / "live-private"
    settings.BACKUP_ROOT = tmp_path / "backups"
    (tmp_path / "backups" / "run").mkdir(parents=True)
    return tmp_path / "backups" / "run"


# --- Guards: nothing is created when any of them refuses ----------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"database": "denstock_live"}, "рабочей или системной"),
        ({"database": "postgres"}, "рабочей или системной"),
        ({"database": "Bad-Name"}, "Имя staging-базы"),
    ],
)
def test_refuses_live_system_or_unsafe_database_names(tmp_path, live_roots, overrides, message):
    with pytest.raises(StageRestoreError, match=message):
        offline_restore._check_targets(live_roots, _targets(tmp_path, **overrides), PG)


def test_refuses_sqlite(tmp_path, live_roots):
    with pytest.raises(StageRestoreError, match="только PostgreSQL"):
        offline_restore._check_targets(
            live_roots, _targets(tmp_path), {"ENGINE": "django.db.backends.sqlite3"}
        )


@pytest.mark.parametrize("field", ["media_dir", "private_media_dir"])
@pytest.mark.parametrize("live", ["live-media", "live-private", "backups"])
def test_refuses_directories_overlapping_live_roots(tmp_path, live_roots, field, live):
    (tmp_path / live).mkdir(exist_ok=True)
    target = _targets(tmp_path, **{field: tmp_path / live / "stage"})
    with pytest.raises(StageRestoreError, match="пересекается"):
        offline_restore._check_targets(live_roots, target, PG)


def test_refuses_existing_directory_or_journal_and_nested_slots(tmp_path, live_roots):
    (tmp_path / "stage-media").mkdir()
    with pytest.raises(StageRestoreError, match="уже существует"):
        offline_restore._check_targets(live_roots, _targets(tmp_path), PG)
    (tmp_path / "stage-media").rmdir()
    (tmp_path / "stage.journal").write_text("")
    with pytest.raises(StageRestoreError, match="Журнал уже существует"):
        offline_restore._check_targets(live_roots, _targets(tmp_path), PG)
    nested = _targets(
        tmp_path, private_media_dir=tmp_path / "stage-media" / "private",
        journal=tmp_path / "other.journal",
    )
    with pytest.raises(StageRestoreError):
        offline_restore._check_targets(live_roots, nested, PG)


def test_journal_is_exclusive_and_append_only(tmp_path):
    journal = offline_restore.Journal(tmp_path / "j")
    journal.record("verified", backup_run_id="x")
    journal.record("failed", failed_stage="restoring_database", error="boom")
    assert [row["stage"] for row in read_journal(tmp_path / "j")] == ["verified", "failed"]
    with pytest.raises(FileExistsError):
        offline_restore.Journal(tmp_path / "j")


def test_command_requires_full_external_inventory(tmp_path):
    with pytest.raises(CommandError, match="--private-media-files"):
        call_command(
            "dr_stage_restore", str(tmp_path), "--target-db", "x", "--media-dir", "m",
            "--private-media-dir", "p", "--journal", "j",
            "--private-media-archive", "a.tar.gz",
        )
    with pytest.raises(CommandError, match="без --private-media-archive"):
        call_command(
            "dr_stage_restore", str(tmp_path), "--target-db", "x", "--media-dir", "m",
            "--private-media-dir", "p", "--journal", "j", "--private-media-files", "1",
        )


def test_discard_requires_yes(tmp_path):
    with pytest.raises(CommandError, match="--yes"):
        call_command("dr_stage_discard", str(tmp_path / "j"))


def test_discard_never_drops_a_database_it_did_not_create(tmp_path, monkeypatch):
    journal = offline_restore.Journal(tmp_path / "j")
    journal.record("creating_database", database="someone_elses_db")
    journal.record("failed", failed_stage="creating_database", error="exists")
    dropped = []
    monkeypatch.setattr(offline_restore, "_drop_database", lambda s, name: dropped.append(name))
    assert discard_stage(tmp_path / "j", settings_dict=PG) == []
    assert dropped == []
    with pytest.raises(StageRestoreError, match="уже удалён"):
        discard_stage(tmp_path / "j", settings_dict=PG)


# --- Real PostgreSQL 16 -------------------------------------------------------

def _require_pg16():
    if connection.vendor != "postgresql":
        pytest.skip("Isolated PostgreSQL 16 required")
    with connection.cursor() as cursor:
        cursor.execute("SHOW server_version_num")
        if int(cursor.fetchone()[0]) // 10000 != 16:
            pytest.skip("Exactly PostgreSQL 16 required")


def _allow_staging_alias(monkeypatch):
    """Django's test case refuses aliases unknown at class setup.

    The staging alias is created on purpose inside the test, so unwrap Django's
    guard back to the method it wraps (pytest-django's, which allows access
    because this test is marked django_db).
    """
    patched = BaseDatabaseWrapper.ensure_connection
    cells = dict(zip(patched.__code__.co_freevars, patched.__closure__ or (), strict=True))
    monkeypatch.setattr(
        BaseDatabaseWrapper, "ensure_connection", cells["real_ensure_connection"].cell_contents,
    )


def _database_exists(name):
    s = connection.settings_dict
    with psycopg.connect(**offline_restore._admin_params(s)) as admin:
        return bool(admin.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s", (name,)
        ).fetchone())


@pytest.fixture
def signed_backup(tmp_path, settings, monkeypatch):
    _require_pg16()
    _allow_staging_alias(monkeypatch)
    configure_test_trust(tmp_path, settings, workstation_id=uuid.uuid4())
    settings.DENSTOCK_MODE = "production"
    settings.DENSTOCK_INSTANCE_ID = "synthetic-pg16"
    settings.DENSTOCK_APP_COMMIT = "d" * 40
    settings.BACKUP_ROOT = tmp_path / "backups"
    media, private = tmp_path / "live-media", tmp_path / "live-private"
    (media / "part-types").mkdir(parents=True)
    (private / "customer_requests" / "7").mkdir(parents=True)
    (media / "part-types" / "photo.jpg").write_bytes(b"synthetic public photo")
    (private / "customer_requests" / "7" / "scan.pdf").write_bytes(b"synthetic private scan")
    settings.MEDIA_ROOT = media
    settings.PRIVATE_MEDIA_ROOT = private
    get_user_model().objects.create(username="synthetic-stage-user")
    PartTypeImage.objects.create(part=build_part(), image="part-types/photo.jpg")
    created = []

    def make():
        return backup.backup_all(
            settings_dict=connection.settings_dict, media_root=media, private_media_root=private,
        )

    yield make, created
    for journal in created:
        if journal.exists():
            try:
                discard_stage(journal)
            except StageRestoreError:
                pass


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_stage_restore_proves_database_and_media_then_discards(tmp_path, signed_backup):
    make, created = signed_backup
    run = make()
    live_users = get_user_model().objects.count()
    targets = _targets(tmp_path, database="dr_stage_" + uuid.uuid4().hex[:8])
    created.append(targets.journal)

    result = stage_restore(run, targets)

    assert result["verdict"] == "staged"
    assert result["proof"]["files"] == {
        "checked": 1, "legacy_public": 0, "missing_count": 0, "missing_sample": [],
    }
    assert (targets.media_dir / "part-types" / "photo.jpg").read_bytes() == (
        b"synthetic public photo"
    )
    assert (targets.private_media_dir / "customer_requests" / "7" / "scan.pdf").is_file()
    stages = [row["stage"] for row in read_journal(targets.journal)]
    assert stages == [
        "verified", "creating_database", "database_created", "database_restored",
        "restoring_media", "media_dir_created", "media_restored",
        "restoring_private_media", "private_media_dir_created", "private_media_restored",
        "staged",
    ]
    # The live database was never touched.
    assert get_user_model().objects.count() == live_users

    removed = discard_stage(targets.journal)
    assert f"database:{targets.database}" in removed
    assert not _database_exists(targets.database)
    assert not targets.media_dir.exists() and not targets.private_media_dir.exists()
    assert read_journal(targets.journal)[-1]["stage"] == "discarded"


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_historical_backup_needs_external_private_media(tmp_path, signed_backup, settings):
    make, created = signed_backup
    run = make()
    manifest = json.loads((run / "manifest.json").read_text())
    expected = Inventory(manifest["private_media_file_count"], manifest["private_media_bytes"],
                         manifest["private_media_tree_sha256"])
    # Reshape into a pre-private-media production backup, re-signed.
    external = tmp_path / "private_media.capture.tar.gz"
    shutil.move(run / "private_media.tar.gz", external)
    for key in ("private_media_status", *PRIVATE_FIELDS):
        manifest.pop(key, None)
    manifest["sha256"].pop("private_media.tar.gz", None)
    manifest.pop("signature")
    sign_production_manifest(manifest, settings)
    (run / "manifest.json").write_text(json.dumps(manifest))

    targets = _targets(tmp_path, database="dr_stage_" + uuid.uuid4().hex[:8])
    created.append(targets.journal)
    with pytest.raises(StageRestoreError, match="не полный DR-набор"):
        stage_restore(run, targets)
    assert not targets.journal.exists()

    wrong = ExternalPrivateMedia(external, Inventory(expected.files + 1, expected.bytes,
                                                     expected.tree_sha256))
    with pytest.raises(StageRestoreError, match="не совпадает с инвентарём"):
        stage_restore(run, targets, private_media=wrong)
    assert not targets.journal.exists() and not _database_exists(targets.database)

    result = stage_restore(run, targets, private_media=ExternalPrivateMedia(external, expected))
    assert result["verdict"] == "staged"
    assert read_journal(targets.journal)[0]["external_private_media"] is True


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_tampered_data_fingerprint_fails_and_is_journalled(tmp_path, signed_backup, settings):
    make, created = signed_backup
    run = make()
    manifest = json.loads((run / "manifest.json").read_text())
    table = next(iter(manifest["data_state"]["tables"]))
    manifest["data_state"]["tables"][table]["count"] += 1
    manifest.pop("signature")
    sign_production_manifest(manifest, settings)
    (run / "manifest.json").write_text(json.dumps(manifest))

    targets = _targets(tmp_path, database="dr_stage_" + uuid.uuid4().hex[:8])
    created.append(targets.journal)
    with pytest.raises(StageRestoreError, match="отпечаток бизнес-данных"):
        stage_restore(run, targets)
    last = read_journal(targets.journal)[-1]
    assert last["stage"] == "failed" and last["failed_stage"] == "verifying_stage"
    discard_stage(targets.journal)
    assert not _database_exists(targets.database)


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_crash_mid_restore_leaves_a_journal_discard_can_follow(
    tmp_path, signed_backup, monkeypatch,
):
    make, created = signed_backup
    run = make()
    targets = _targets(tmp_path, database="dr_stage_" + uuid.uuid4().hex[:8])
    created.append(targets.journal)

    def disk_full(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(backup, "restore_media", disk_full)
    with pytest.raises(StageRestoreError, match="restoring_media"):
        stage_restore(run, targets)
    rows = read_journal(targets.journal)
    assert rows[-1]["failed_stage"] == "restoring_media"
    assert _database_exists(targets.database)
    removed = discard_stage(targets.journal)
    assert removed == [f"database:{targets.database}", f"dir:{targets.media_dir.resolve()}"]
    assert not _database_exists(targets.database) and not targets.media_dir.exists()


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_missing_referenced_file_needs_review(tmp_path, signed_backup):
    make, created = signed_backup
    PartTypeImage.objects.create(
        part=PartTypeImage.objects.get().part, image="part-types/ghost.jpg",
    )
    run = make()
    targets = _targets(tmp_path, database="dr_stage_" + uuid.uuid4().hex[:8])
    created.append(targets.journal)
    result = stage_restore(run, targets)
    assert result["verdict"] == "staged_needs_review"
    assert result["proof"]["files"]["missing_sample"] == [
        "catalog.parttypeimage.image: part-types/ghost.jpg"
    ]
