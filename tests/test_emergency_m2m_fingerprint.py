"""Regression: изменение роли обязано менять отпечаток бизнес-состояния.

Промежуточные таблицы связей «многие ко многим» Django создаёт автоматически,
поэтому раньше они выпадали из отпечатка: он строился с
`include_auto_created=False`, а защита записи с `include_auto_created=True`.
В проекте таких таблиц три. Две принадлежат приложению `accounts`: членство
пользователя в группах (то есть РОЛИ) и персональные права. Третья,
`auth.group_permissions`, относится к приложению `auth`, которое бизнес-данными
не считается, и в отпечаток осознанно не входит: её конечные модели `auth.group`
и `auth.permission` в отпечатке тоже отсутствуют, включать одну связь без них
было бы непоследовательно.

Здесь закрепляется исправленное поведение, граница включения и то, что формат
маркера при этом не изменился.
"""
import hashlib
import json
from copy import deepcopy

import pytest
from django.apps import apps as django_apps
from django.contrib.auth.models import Group, Permission
from django.utils import timezone

from apps.accounts import roles
from apps.operations.emergency_manifest import SCHEMA_VERSION, validate_manifest
from apps.operations.emergency_state import (
    BUSINESS_APP_LABELS,
    business_state_marker,
    sha256_file,
)
from apps.operations.failback import evaluate_failback
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.write_guard import BusinessWriteBlocked, _is_business_mutation

MEMBERSHIP = "accounts.user_groups"
PERMISSIONS = "accounts.user_user_permissions"
FRAMEWORK = "auth.group_permissions"


def _sha():
    return business_state_marker()["business_sha256"]


def _generation():
    return DeploymentState.objects.get(pk=DeploymentState.SINGLETON_PK).business_generation


def _set_state(write_state):
    state = DeploymentState.get_solo()
    state.write_state = write_state
    state.save(update_fields=["write_state", "updated_at"])


@pytest.fixture
def production_mode(db, settings):
    """Обычный production: защита записи включена, запись разрешена."""
    _set_state(DeploymentState.WriteState.NORMAL)
    settings.DENSTOCK_MODE = "production"
    yield
    settings.DENSTOCK_MODE = "test"


# --- A. Добавление роли ---------------------------------------------------------------------


def test_adding_group_changes_fingerprint(db, django_user_model):
    """Пользователь без группы, затем с группой: отпечаток обязан отличаться."""
    user = django_user_model.objects.create_user(username="worker", password="x")
    before = _sha()
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    after = _sha()
    assert before != after


def test_adding_user_permission_changes_fingerprint(db, django_user_model):
    """Вторая промежуточная таблица accounts тоже обязана учитываться."""
    user = django_user_model.objects.create_user(username="worker2", password="x")
    before = _sha()
    user.user_permissions.add(Permission.objects.first())
    assert _sha() != before


# --- B. Снятие роли -------------------------------------------------------------------------


def test_removing_group_changes_fingerprint(db, django_user_model):
    """Снятие роли тоже обязано быть видно, а не только выдача."""
    user = django_user_model.objects.create_user(username="worker3", password="x")
    empty = _sha()
    group = Group.objects.get(name=roles.SELLER)
    user.groups.add(group)
    with_group = _sha()
    user.groups.remove(group)
    without_group = _sha()

    assert with_group != empty
    assert without_group != with_group
    # Отпечаток это чистая функция данных: строка связи удалена, значит состояние
    # вернулось к исходному и хеш обязан совпасть с исходным.
    assert without_group == empty


# --- C. Детерминизм -------------------------------------------------------------------------


def test_marker_is_deterministic_without_changes(db, django_user_model):
    user = django_user_model.objects.create_user(username="worker4", password="x")
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    first = business_state_marker()
    second = business_state_marker()
    assert first["business_sha256"] == second["business_sha256"]
    assert first["tables"] == second["tables"]


def test_membership_table_marker_is_stable_across_calls(db, django_user_model):
    user = django_user_model.objects.create_user(username="worker5", password="x")
    user.groups.add(Group.objects.get(name=roles.VIEWER))
    first = business_state_marker()["tables"][MEMBERSHIP]
    second = business_state_marker()["tables"][MEMBERSHIP]
    assert first == second
    assert first["count"] == 1


# --- D. Оба признака ------------------------------------------------------------------------


def test_role_change_moves_both_signals(production_mode, django_user_model):
    """Отпечаток и счётчик поколений обязаны сработать независимо друг от друга."""
    user = django_user_model.objects.create_user(username="worker6", password="x")
    sha_before = _sha()
    generation_before = _generation()
    user.groups.add(Group.objects.get(name=roles.SELLER))
    assert _sha() != sha_before, "отпечаток не увидел смену роли"
    assert _generation() > generation_before, "счётчик поколений не увидел смену роли"


# --- Граница включения ----------------------------------------------------------------------


def test_marker_lists_accounts_membership_tables(db):
    tables = business_state_marker()["tables"]
    assert MEMBERSHIP in tables
    assert PERMISSIONS in tables


def test_framework_group_permissions_stays_out_of_marker(db):
    """Связь auth.group_permissions намеренно не входит: auth не бизнес-приложение.

    Это граница осознанного включения, а не побочный эффект: её конечные модели
    auth.group и auth.permission в отпечатке тоже отсутствуют.
    """
    tables = business_state_marker()["tables"]
    assert FRAMEWORK not in tables
    assert "auth" not in BUSINESS_APP_LABELS
    assert not any(name.startswith("auth.") for name in tables)


def test_group_permission_change_does_not_touch_fingerprint(db):
    """Контроль границы: изменение прав ГРУППЫ отпечаток не двигает."""
    group = Group.objects.get(name=roles.VIEWER)
    before = _sha()
    group.permissions.add(Permission.objects.first())
    assert _sha() == before


def test_every_fingerprinted_table_is_also_write_guarded(db):
    """Отпечаток и защита записи обязаны покрывать один и тот же набор таблиц.

    Именно рассогласование этих двух наборов и было исходной причиной пробела.
    Тест проверяет симметрию через фактическое поведение защиты, а не через
    повторение того же выражения выбора моделей.
    """
    for label in business_state_marker()["tables"]:
        table = django_apps.get_model(label)._meta.db_table
        sql = f'INSERT INTO "{table}" ("id") VALUES (1)'
        assert _is_business_mutation(sql), (
            f"таблица {table} входит в отпечаток, но не охраняется защитой записи"
        )


def test_framework_table_is_neither_fingerprinted_nor_guarded(db):
    """Обратная сторона симметрии: чего нет в отпечатке, того нет и в защите."""
    table = django_apps.get_model(FRAMEWORK)._meta.db_table
    assert FRAMEWORK not in business_state_marker()["tables"]
    assert not _is_business_mutation(f'INSERT INTO "{table}" ("id") VALUES (1)')


# --- E. Failback conflict -------------------------------------------------------------------


COMMIT = "a" * 40
MIGRATION_HASH = hashlib.sha256(b"[]").hexdigest()
DATABASE_ID = "52347a14-d939-45e6-a397-06c79ef257f2"
BASE_RUN_ID = "d7919779-6c24-43cb-bb78-181f61a335d5"


def _final_run(root, session, media_hash):
    run = root / "2026-08-12_12-00-00"
    run.mkdir(parents=True, exist_ok=True)
    database = run / "db.dump"
    database.write_bytes(b"verified-final-dump")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "backup_run_id": "155f5606-813a-496c-b699-3554e39a96ea",
        "created_at": "2026-08-12T12:00:00+05:00",
        "verified_at": "2026-08-12T12:01:00+05:00",
        "source_environment": "emergency-local",
        "source_instance_id": session.instance_id,
        "app_commit": COMMIT,
        "database_name": "denstock_emergency_local",
        "database_identity": DATABASE_ID,
        "database_dump_filename": database.name,
        "database_sha256": sha256_file(database),
        "media_filename": None,
        "media_sha256": None,
        "media_tree_sha256": media_hash,
        "migration_fingerprint": MIGRATION_HASH,
        "migration_state": [],
        "data_state": {
            "database_identity": DATABASE_ID,
            "business_generation": 20,
            "business_sha256": "f" * 64,
            "tables": {},
        },
        "storage_origin": "emergency-local",
        "verification_status": "verified",
        "consistency": "single_writer_locked",
        "type": "emergency_final",
        "offline_lineage": {
            "offline_session_id": str(session.id),
            "base_backup_run_id": BASE_RUN_ID,
            "base_database_identity": DATABASE_ID,
            "base_business_sha256": session.base_data_marker["business_sha256"],
            "base_media_sha256": media_hash,
        },
    }
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return run


MEDIA_HASH = "e" * 64


def _session(base_marker):
    return OfflineSession.objects.create(
        kind=OfflineSession.Kind.UNPLANNED,
        status=OfflineSession.Status.FROZEN,
        local_hostname="warehouse-pc",
        instance_id="warehouse-pc",
        base_backup_run_id=BASE_RUN_ID,
        base_backup_created_at=timezone.now(),
        base_manifest={
            "backup_run_id": BASE_RUN_ID,
            "source_instance_id": "production",
            "database_identity": DATABASE_ID,
            "app_commit": COMMIT,
            "migration_fingerprint": MIGRATION_HASH,
        },
        base_data_marker=base_marker,
        base_media_sha256=MEDIA_HASH,
        base_app_commit=COMMIT,
        base_migration_fingerprint=MIGRATION_HASH,
    )


def _probe(marker, *, app_commit=COMMIT):
    return {
        "schema_version": 1,
        "mode": "production",
        "instance_id": "production",
        "write_state": DeploymentState.WriteState.MAINTENANCE,
        "state_reason": "controlled failover",
        "stable_snapshot": True,
        "app_commit": app_commit,
        "migration_fingerprint": MIGRATION_HASH,
        "data_state": marker,
        "media_tree_sha256": MEDIA_HASH,
    }


def _marker_as(identity=DATABASE_ID, **overrides):
    marker = deepcopy(business_state_marker())
    marker["database_identity"] = identity
    marker.update(overrides)
    return marker


def test_production_role_change_produces_failback_conflict(db, django_user_model, tmp_path):
    """Смена роли на production обязана дать conflict по одному лишь отпечатку.

    Счётчик поколений в тесте намеренно выровнен между base и production, чтобы
    доказать, что отпечаток стал САМОСТОЯТЕЛЬНЫМ признаком, а не просто дублирует
    уже существующий сигнал.
    """
    base_marker = _marker_as()
    session = _session(base_marker)
    run = _final_run(tmp_path, session, MEDIA_HASH)

    # Production выдал сотруднику роль, пока склад работал автономно.
    user = django_user_model.objects.create_user(username="production-worker", password="x")
    user.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    production_marker = _marker_as(business_generation=base_marker["business_generation"])

    assert production_marker["business_sha256"] != base_marker["business_sha256"]

    decision = evaluate_failback(session, _probe(production_marker), run)

    assert decision.status == OfflineSession.Status.CONFLICT
    assert not decision.eligible
    assert any("fingerprint" in reason for reason in decision.reasons)
    assert MEMBERSHIP in decision.differences["business_tables"]


def test_unchanged_production_still_reports_no_conflict(db, tmp_path):
    """Контроль: без изменений роли отпечаток conflict не выдумывает."""
    base_marker = _marker_as()
    session = _session(base_marker)
    run = _final_run(tmp_path, session, MEDIA_HASH)

    decision = evaluate_failback(session, _probe(_marker_as()), run)

    assert decision.status == OfflineSession.Status.ELIGIBLE
    assert decision.eligible


def test_markers_from_different_app_commits_are_never_silently_compared(db, tmp_path):
    """Опора вывода о совместимости со старыми резервными копиями.

    Состав таблиц отпечатка задан кодом, поэтому маркер, посчитанный прежней
    версией, и маркер новой версии сравнивать нельзя. Это и не происходит: любое
    сравнение business_sha256 предварено проверкой равенства app_commit, а
    выкладка изменения обязательно меняет commit. Если эту проверку когда-нибудь
    снимут, тест упадёт и не даст молча сравнить несравнимое.
    """
    base_marker = _marker_as()
    session = _session(base_marker)
    run = _final_run(tmp_path, session, MEDIA_HASH)

    decision = evaluate_failback(
        session, _probe(_marker_as(), app_commit="b" * 40), run
    )

    assert decision.status == OfflineSession.Status.BLOCKED
    assert any("application commit" in reason for reason in decision.reasons)


# --- F. Заморозка ---------------------------------------------------------------------------


def test_frozen_emergency_blocks_group_membership_write(db, settings, django_user_model):
    """Заморозка обязана блокировать и запись в промежуточную таблицу ролей."""
    user = django_user_model.objects.create_user(username="worker7", password="x")
    group = Group.objects.get(name=roles.STOREKEEPER)
    _set_state(DeploymentState.WriteState.EMERGENCY_FROZEN)
    settings.DENSTOCK_MODE = "emergency-local"
    try:
        with pytest.raises(BusinessWriteBlocked):
            user.groups.add(group)
    finally:
        settings.DENSTOCK_MODE = "test"


# --- G. Существующее покрытие не пострадало --------------------------------------------------


def test_existing_business_models_are_still_covered(db):
    """Обычные бизнес-таблицы обязаны остаться в отпечатке."""
    tables = business_state_marker()["tables"]
    for prefix in ("customers.", "catalog_import.", "sales.", "inventory.", "warehouse."):
        assert any(name.startswith(prefix) for name in tables), prefix


def test_customer_change_still_moves_fingerprint(db):
    from apps.customers.models import Customer

    before = _sha()
    Customer.objects.create(name="Иванов", phone="+79121234567")
    assert _sha() != before


# --- Совместимость --------------------------------------------------------------------------


def test_marker_structure_keys_are_unchanged(db):
    """Формат маркера не менялся: те же ключи, изменился только состав tables."""
    marker = business_state_marker()
    assert set(marker) == {
        "database_identity",
        "business_generation",
        "business_sha256",
        "tables",
    }
    sample = marker["tables"][MEMBERSHIP]
    assert set(sample) == {"count", "max_pk", "sha256"}


def test_legacy_manifest_without_membership_tables_still_validates(tmp_path):
    """Старые резервные копии обязаны оставаться валидными.

    Маркер в них перечисляет только таблицы, известные прежней версии. Валидатор
    не хранит фиксированный список, поэтому переписывать исторические копии не
    требуется.
    """
    database = tmp_path / "db.dump"
    database.write_bytes(b"database")
    legacy = {
        "schema_version": SCHEMA_VERSION,
        "backup_run_id": "a63f4a56-a616-4a6d-ad1d-a7bace93130f",
        "created_at": "2026-08-12T10:00:00+05:00",
        "verified_at": "2026-08-12T10:01:00+05:00",
        "source_environment": "production",
        "source_instance_id": "production",
        "app_commit": "a" * 40,
        "database_name": "denstock",
        "database_identity": DATABASE_ID,
        "database_dump_filename": database.name,
        "database_sha256": sha256_file(database),
        "media_filename": None,
        "media_sha256": None,
        "media_tree_sha256": "b" * 64,
        "migration_fingerprint": hashlib.sha256(b"[]").hexdigest(),
        "migration_state": [],
        "data_state": {
            "database_identity": DATABASE_ID,
            "business_generation": 7,
            "business_sha256": "d" * 64,
            # Ровно то, что записала бы прежняя версия: связей membership нет.
            "tables": {
                "customers.customer": {"count": 3, "max_pk": 3, "sha256": "1" * 64},
                "sales.sale": {"count": 2, "max_pk": 2, "sha256": "2" * 64},
            },
        },
        "storage_origin": "yandex-object-storage",
        "verification_status": "verified",
        "consistency": "database_snapshot",
    }
    (tmp_path / "manifest.json").write_text(json.dumps(legacy), encoding="utf-8")

    report = validate_manifest(tmp_path, expected_source="production")

    assert report.ok
    assert report.errors == []
