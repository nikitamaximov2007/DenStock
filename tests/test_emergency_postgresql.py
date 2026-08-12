import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal

import pytest
from django.db import close_old_connections, connection, connections, transaction

from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockLot
from apps.operations import backup
from apps.operations.emergency_lifecycle import (
    EmergencyLifecycleError,
    start_offline_session,
)
from apps.operations.emergency_manifest import validate_manifest
from apps.operations.emergency_state import business_state_marker, migration_state
from apps.operations.failback import FailbackError, _freeze_for_export
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.standby import (
    EmergencyPaths,
    create_database,
    drop_database,
    save_control,
    validate_candidate,
)
from apps.operations.write_guard import (
    BusinessWriteBlocked,
    acquire_failover_lock,
    lifecycle_write,
)
from apps.receipts.models import Receipt
from apps.receipts.services import add_line, create_receipt, post_receipt
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

pytestmark = pytest.mark.postgresql

if connection.vendor != "postgresql":
    pytest.skip(
        "Run against PostgreSQL 16 with DENSTOCK_TEST_DATABASE_URL",
        allow_module_level=True,
    )

COMMIT = os.environ.get("DENSTOCK_APP_COMMIT", "a" * 40)


def _thread_call(callback):
    close_old_connections()
    try:
        return callback()
    except Exception as exc:  # noqa: BLE001 - assertions inspect the exact result.
        return exc
    finally:
        close_old_connections()


def _set_state(write_state):
    with lifecycle_write():
        state = DeploymentState.get_solo()
        state.write_state = write_state
        state.save(update_fields=["write_state", "updated_at"])
    return state


@contextmanager
def _using_database(name):
    database = connections["default"]
    original = database.settings_dict["NAME"]
    database.close()
    database.settings_dict["NAME"] = name
    try:
        yield
    finally:
        database.close()
        database.settings_dict["NAME"] = original


@pytest.mark.django_db(transaction=True)
def test_postgresql_freeze_blocks_a_write_that_arrives_after_exclusive_lock(settings):
    _set_state(DeploymentState.WriteState.NORMAL)
    settings.DENSTOCK_MODE = "production"
    lock_acquired = threading.Event()
    allow_freeze = threading.Event()

    def freeze():
        with transaction.atomic(), lifecycle_write():
            acquire_failover_lock(exclusive=True)
            state = DeploymentState.objects.select_for_update().get(pk=1)
            lock_acquired.set()
            assert allow_freeze.wait(10)
            state.write_state = DeploymentState.WriteState.MAINTENANCE
            state.save(update_fields=["write_state", "updated_at"])
        return "frozen"

    def write():
        assert lock_acquired.wait(10)
        return Supplier.objects.create(name="Must be blocked")

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            freeze_future = pool.submit(_thread_call, freeze)
            assert lock_acquired.wait(10)
            write_future = pool.submit(_thread_call, write)
            time.sleep(0.25)
            assert not write_future.done()
            allow_freeze.set()
            assert freeze_future.result(timeout=10) == "frozen"
            result = write_future.result(timeout=10)
        assert isinstance(result, BusinessWriteBlocked)
        assert not Supplier.objects.filter(name="Must be blocked").exists()
    finally:
        settings.DENSTOCK_MODE = "test"


@pytest.mark.django_db(transaction=True)
def test_postgresql_freeze_waits_for_an_already_running_write_transaction(settings):
    _set_state(DeploymentState.WriteState.NORMAL)
    settings.DENSTOCK_MODE = "production"
    write_done = threading.Event()
    allow_commit = threading.Event()

    def write():
        with transaction.atomic():
            Supplier.objects.create(name="Committed before freeze")
            write_done.set()
            assert allow_commit.wait(10)
        return "committed"

    def freeze():
        assert write_done.wait(10)
        with transaction.atomic(), lifecycle_write():
            acquire_failover_lock(exclusive=True)
            state = DeploymentState.objects.select_for_update().get(pk=1)
            state.write_state = DeploymentState.WriteState.MAINTENANCE
            state.save(update_fields=["write_state", "updated_at"])
        return "frozen"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            write_future = pool.submit(_thread_call, write)
            assert write_done.wait(10)
            freeze_future = pool.submit(_thread_call, freeze)
            time.sleep(0.25)
            assert not freeze_future.done()
            allow_commit.set()
            assert write_future.result(timeout=10) == "committed"
            assert freeze_future.result(timeout=10) == "frozen"
        assert Supplier.objects.filter(name="Committed before freeze").exists()
        assert DeploymentState.get_solo().write_state == (
            DeploymentState.WriteState.MAINTENANCE
        )
    finally:
        settings.DENSTOCK_MODE = "test"


@pytest.mark.django_db(transaction=True)
def test_postgresql_two_start_commands_have_one_winner(tmp_path, settings, monkeypatch):
    state = _set_state(DeploymentState.WriteState.NORMAL)
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "postgres-start-test"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    settings.DENSTOCK_EMERGENCY_DB_PREFIX = str(connection.settings_dict["NAME"])
    settings.DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS = [connection.settings_dict["HOST"]]
    settings.DENSTOCK_PRODUCTION_DB_HOSTS = []
    marker = business_state_marker()
    migrations = migration_state()
    manifest = {
        "backup_run_id": str(uuid.uuid4()),
        "created_at": "2026-08-12T10:00:00+05:00",
        "app_commit": COMMIT,
        "database_identity": str(state.database_identity),
        "migration_fingerprint": migrations["fingerprint"],
        "media_tree_sha256": "d" * 64,
        "data_state": marker,
        "consistency": "database_snapshot",
    }
    paths = EmergencyPaths(tmp_path / "emergency")
    save_control(
        {
            "active_standby": {
                "database_name": connection.settings_dict["NAME"],
                "backup_run_id": manifest["backup_run_id"],
            },
            "previous_standbys": [],
        },
        paths,
    )
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: ({"database_name": connection.settings_dict["NAME"]}, manifest),
    )

    def start():
        return start_offline_session(
            kind=OfflineSession.Kind.UNPLANNED,
            paths=paths,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: _thread_call(start), range(2)))
        assert sum(isinstance(result, OfflineSession) for result in results) == 1
        assert sum(isinstance(result, EmergencyLifecycleError) for result in results) == 1
        assert OfflineSession.objects.filter(status=OfflineSession.Status.ACTIVE).count() == 1
    finally:
        settings.DENSTOCK_MODE = "test"


@pytest.mark.django_db(transaction=True)
def test_postgresql_two_stop_transitions_have_one_winner(settings):
    settings.DENSTOCK_MODE = "test"
    state = _set_state(DeploymentState.WriteState.EMERGENCY_ACTIVE)
    session = OfflineSession.objects.create(
        kind=OfflineSession.Kind.UNPLANNED,
        status=OfflineSession.Status.ACTIVE,
        local_hostname="postgres-test",
        instance_id="postgres-stop-test",
        base_backup_run_id="base",
        base_backup_created_at="2026-08-12T10:00:00+05:00",
        base_manifest={},
        base_data_marker={},
        base_migration_fingerprint="b" * 64,
    )
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_EMERGENCY_DB_PREFIX = str(connection.settings_dict["NAME"])
    settings.DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS = [connection.settings_dict["HOST"]]
    settings.DENSTOCK_PRODUCTION_DB_HOSTS = []

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(lambda _: _thread_call(lambda: _freeze_for_export()), range(2))
            )
        assert sum(isinstance(result, OfflineSession) for result in results) == 1
        assert sum(isinstance(result, FailbackError) for result in results) == 1
        session.refresh_from_db()
        state.refresh_from_db()
        assert session.status == OfflineSession.Status.FREEZING
        assert state.write_state == DeploymentState.WriteState.EMERGENCY_FROZEN
    finally:
        settings.DENSTOCK_MODE = "test"


@pytest.mark.django_db(transaction=True)
def test_postgresql_two_stage_backup_restore_and_offline_warehouse_operation(
    tmp_path, settings, django_user_model
):
    settings.DENSTOCK_MODE = "test"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    settings.DENSTOCK_INSTANCE_ID = "postgres-integration"
    settings.DENSTOCK_EMERGENCY_DB_PREFIX = "denstock_emergency_it_"
    settings.DENSTOCK_EMERGENCY_ALLOWED_DB_HOSTS = [connection.settings_dict["HOST"]]
    settings.DENSTOCK_PRODUCTION_DB_HOSTS = []
    _set_state(DeploymentState.WriteState.NORMAL)
    user = django_user_model.objects.create_superuser(
        username="postgres-emergency", password="test"
    )
    supplier = Supplier.objects.create(name="PostgreSQL source supplier")
    category = Category.objects.create(name="PostgreSQL source category")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    part = PartType.objects.create(
        name="PostgreSQL source part",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    location = StorageLocation.objects.create(
        code="S91-D01-C01", name="PostgreSQL source location", storage_allowed=True
    )
    source_media = tmp_path / "source-media"
    source_media.mkdir()
    (source_media / "source.txt").write_text("source-media", encoding="utf-8")
    first_database = f"denstock_emergency_it_{uuid.uuid4().hex[:12]}"
    second_database = f"denstock_emergency_it_{uuid.uuid4().hex[:12]}"
    created_databases = []
    original_database = connection.settings_dict["NAME"]

    try:
        settings.DENSTOCK_MODE = "production"
        source_run = backup.backup_all(
            root=tmp_path / "source-backups",
            media_root=source_media,
            trigger="automatic",
        )
        source_manifest = validate_manifest(source_run, expected_source="production")
        assert source_manifest.ok

        create_database(first_database)
        created_databases.append(first_database)
        first_settings = {**connection.settings_dict, "NAME": first_database}
        backup.restore_db(
            source_run / source_manifest.manifest["database_dump_filename"],
            settings_dict=first_settings,
        )
        first_media = tmp_path / "first-media"
        backup.restore_media(
            source_run / source_manifest.manifest["media_filename"],
            media_root=first_media,
        )
        first_probe = validate_candidate(first_database, first_media)
        assert first_probe["data_state"]["business_sha256"] == (
            source_manifest.manifest["data_state"]["business_sha256"]
        )

        with _using_database(first_database):
            settings.DENSTOCK_MODE = "emergency-local"
            _set_state(DeploymentState.WriteState.EMERGENCY_ACTIVE)
            restored_user = django_user_model.objects.get(username=user.username)
            restored_supplier = Supplier.objects.get(pk=supplier.pk)
            restored_part = PartType.objects.get(pk=part.pk)
            restored_location = StorageLocation.objects.get(pk=location.pk)
            receipt = create_receipt(supplier=restored_supplier, by=restored_user)
            add_line(
                receipt,
                part_type=restored_part,
                quantity=Decimal("3"),
                unit_cost_rub=Decimal("125"),
                location=restored_location,
            )
            receipt = post_receipt(receipt, by=restored_user)
            assert receipt.status == Receipt.Status.POSTED
            assert StockLot.objects.filter(batch=receipt.batch, quantity=Decimal("3")).exists()
            (first_media / "offline.txt").write_text("offline-media", encoding="utf-8")
            _set_state(DeploymentState.WriteState.EMERGENCY_FROZEN)
            final_run = backup.backup_all(
                root=tmp_path / "final-backups",
                media_root=first_media,
                trigger="emergency_final",
            )
            final_manifest = validate_manifest(
                final_run, expected_source="emergency-local"
            )
            assert final_manifest.ok

        assert connection.settings_dict["NAME"] == original_database
        settings.DENSTOCK_MODE = "test"
        create_database(second_database)
        created_databases.append(second_database)
        second_settings = {**connection.settings_dict, "NAME": second_database}
        backup.restore_db(
            final_run / final_manifest.manifest["database_dump_filename"],
            settings_dict=second_settings,
        )
        second_media = tmp_path / "second-media"
        backup.restore_media(
            final_run / final_manifest.manifest["media_filename"],
            media_root=second_media,
        )
        second_probe = validate_candidate(second_database, second_media)
        assert second_probe["data_state"]["business_sha256"] == (
            final_manifest.manifest["data_state"]["business_sha256"]
        )
        assert (second_media / "source.txt").read_text(encoding="utf-8") == "source-media"
        assert (second_media / "offline.txt").read_text(encoding="utf-8") == "offline-media"
        with _using_database(second_database):
            settings.DENSTOCK_MODE = "test"
            assert Receipt.objects.filter(status=Receipt.Status.POSTED).count() == 1
            assert StockLot.objects.filter(quantity=Decimal("3")).exists()
    finally:
        connections["default"].close()
        connections["default"].settings_dict["NAME"] = original_database
        settings.DENSTOCK_MODE = "test"
        for database_name in reversed(created_databases):
            drop_database(database_name)
