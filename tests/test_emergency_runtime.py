import uuid
from datetime import timedelta

import pytest
from django.db import transaction
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.ai_support.services import FeatureDisabled, send_message
from apps.ai_support.views import _provider_state
from apps.operations.emergency_lifecycle import (
    EmergencyLifecycleError,
    emergency_context,
    start_offline_session,
)
from apps.operations.emergency_environment import EmergencySafetyError, configured_workstation_id
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.standby import EmergencyPaths, save_control
from apps.operations.write_guard import BusinessWriteBlocked, BusinessWriteGuardMiddleware
from apps.suppliers.models import Supplier
from tests.emergency_support import configure_test_trust

COMMIT = "a" * 40
MIGRATION_HASH = "b" * 64
DATA_HASH = "c" * 64
DATABASE_ID = "52347a14-d939-45e6-a397-06c79ef257f2"


def _base_manifest(*, consistency="database_snapshot", workstation_id=None):
    return {
        "backup_run_id": "d7919779-6c24-43cb-bb78-181f61a335d5",
        "created_at": (timezone.now() - timedelta(hours=2)).isoformat(),
        "app_commit": COMMIT,
        "database_identity": DATABASE_ID,
        "migration_fingerprint": MIGRATION_HASH,
        "media_tree_sha256": "d" * 64,
        "data_state": {"business_sha256": DATA_HASH},
        "authorized_emergency_primary_id": str(workstation_id),
        "primary_authorization_epoch": 1,
        "consistency": consistency,
    }


@pytest.fixture
def lifecycle_runtime(tmp_path, monkeypatch, settings):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "warehouse-pc-test"
    settings.DENSTOCK_APP_COMMIT = COMMIT
    workstation_id = uuid.uuid4()
    configure_test_trust(tmp_path, settings, workstation_id=workstation_id)
    state = DeploymentState.get_solo()
    state.database_identity = DATABASE_ID
    state.write_state = DeploymentState.WriteState.NORMAL
    state.save()
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle.validate_database_target", lambda **kwargs: None
    )
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle.migration_state",
        lambda: {"fingerprint": MIGRATION_HASH},
    )
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle.business_state_marker",
        lambda: {"business_sha256": DATA_HASH, "database_identity": DATABASE_ID},
    )
    monkeypatch.setattr("apps.operations.emergency_lifecycle.backup._git_commit", lambda: COMMIT)
    settings.DENSTOCK_EMERGENCY_ROOT = tmp_path / "emergency"
    save_control(
        {
            "active_standby": {
                "database_name": "ignored",
                "backup_run_id": _base_manifest(workstation_id=workstation_id)["backup_run_id"],
            },
            "previous_standbys": [],
        },
        EmergencyPaths(settings.DENSTOCK_EMERGENCY_ROOT),
    )
    return state, workstation_id


@pytest.mark.django_db
def test_start_from_verified_standby_creates_active_session(lifecycle_runtime, monkeypatch):
    state, workstation_id = lifecycle_runtime
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: (
            {"database_name": "ignored"},
            _base_manifest(workstation_id=workstation_id),
        ),
    )

    session = start_offline_session(kind=OfflineSession.Kind.UNPLANNED, actor="operator")

    state.refresh_from_db()
    assert session.status == OfflineSession.Status.ACTIVE
    assert session.base_data_marker == {"business_sha256": DATA_HASH}
    assert state.write_state == DeploymentState.WriteState.EMERGENCY_ACTIVE


@pytest.mark.django_db
def test_two_start_commands_are_serialized_and_second_is_denied(lifecycle_runtime, monkeypatch):
    _, workstation_id = lifecycle_runtime
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: (
            {"database_name": "ignored"},
            _base_manifest(workstation_id=workstation_id),
        ),
    )
    start_offline_session(kind=OfflineSession.Kind.UNPLANNED)

    with pytest.raises(EmergencyLifecycleError, match="lifecycle"):
        start_offline_session(kind=OfflineSession.Kind.UNPLANNED)


@pytest.mark.django_db
def test_planned_start_requires_maintenance_consistent_backup(lifecycle_runtime, monkeypatch):
    _, workstation_id = lifecycle_runtime
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: (
            {"database_name": "ignored"},
            _base_manifest(workstation_id=workstation_id),
        ),
    )

    with pytest.raises(EmergencyLifecycleError, match="maintenance lock"):
        start_offline_session(kind=OfflineSession.Kind.PLANNED)


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="production")
def test_direct_business_write_is_blocked_in_maintenance():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.MAINTENANCE
    state.save()

    with pytest.raises(BusinessWriteBlocked):
        Supplier.objects.create(name="Blocked supplier")


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local")
def test_business_write_requires_active_local_session():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.NORMAL
    state.save()
    with pytest.raises(BusinessWriteBlocked), transaction.atomic():
        Supplier.objects.create(name="Standby write")

    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    supplier = Supplier.objects.create(name="Offline supplier")
    assert supplier.pk


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local", DENSTOCK_EMERGENCY_ROLE="secondary")
def test_secondary_workstation_cannot_activate_a_second_emergency_writer(lifecycle_runtime):
    with pytest.raises(EmergencyLifecycleError, match="secondary"):
        start_offline_session(kind=OfflineSession.Kind.UNPLANNED)


def test_protected_workstation_identity_rejects_copied_environment(tmp_path, settings):
    first, second = uuid.uuid4(), uuid.uuid4()
    identity = tmp_path / "workstation-id.txt"
    identity.write_text(str(second), encoding="utf-8")
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(first)
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH = str(identity)

    with pytest.raises(EmergencySafetyError, match="does not match"):
        configured_workstation_id()


def test_protected_workstation_identity_survives_env_validation(tmp_path, settings):
    workstation_id = uuid.uuid4()
    identity = tmp_path / "workstation-id.txt"
    identity.write_text(str(workstation_id), encoding="utf-8")
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(workstation_id)
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID_PATH = str(identity)

    assert configured_workstation_id() == workstation_id


@pytest.mark.django_db
def test_two_workstations_require_the_production_authorized_identity(
    lifecycle_runtime, monkeypatch, settings
):
    _, primary_id = lifecycle_runtime
    other_id = uuid.uuid4()
    manifest = _base_manifest(workstation_id=primary_id)
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: ({"database_name": "ignored"}, manifest),
    )

    # B may set its local role to primary, but production authorization remains for A.
    settings.DENSTOCK_EMERGENCY_ROLE = "primary"
    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(other_id)
    with pytest.raises(EmergencyLifecycleError, match="другого аварийного"):
        start_offline_session(kind=OfflineSession.Kind.UNPLANNED)

    settings.DENSTOCK_EMERGENCY_WORKSTATION_ID = str(primary_id)
    session = start_offline_session(kind=OfflineSession.Kind.UNPLANNED)
    assert session.status == OfflineSession.Status.ACTIVE


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="production")
def test_http_write_updates_generation_only_after_business_mutation():
    factory = RequestFactory()
    DeploymentState.get_solo()

    def response_with_write(request):
        Supplier.objects.create(name="Generation supplier")
        return HttpResponse("ok")

    middleware = BusinessWriteGuardMiddleware(response_with_write)
    response = middleware(factory.post("/suppliers/new/"))

    assert response.status_code == 200
    assert DeploymentState.get_solo().business_generation == 1


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="production")
def test_http_write_returns_locked_when_production_is_frozen():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.MAINTENANCE
    state.save()
    middleware = BusinessWriteGuardMiddleware(lambda request: HttpResponse("unexpected"))

    response = middleware(RequestFactory().post("/suppliers/new/"))

    assert response.status_code == 423


def test_http_write_guard_does_not_open_database_in_test_mode():
    middleware = BusinessWriteGuardMiddleware(lambda request: HttpResponse("ok"))

    response = middleware(RequestFactory().post("/scanner/resolve/"))

    assert response.status_code == 200


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local", AI_SUPPORT_ENABLED=True)
def test_ai_support_is_gracefully_unavailable_offline():
    assert _provider_state() == "offline"
    with pytest.raises(FeatureDisabled):
        send_message(conversation=None, user=None, text="question", token=None)


@pytest.mark.django_db
def test_ai_support_page_explains_offline_unavailability(client, django_user_model):
    user = django_user_model.objects.create_superuser(username="offline-admin", password="test")
    client.force_login(user)

    with override_settings(DENSTOCK_MODE="emergency-local", AI_SUPPORT_ENABLED=True):
        response = client.get(reverse("ai_support:home"))

    assert response.status_code == 200
    assert "Недоступно в автономном режиме" in response.content.decode("utf-8")


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local", DENSTOCK_INSTANCE_ID="warehouse-pc")
def test_emergency_banner_is_visible_on_authenticated_pages(client, django_user_model):
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    user = django_user_model.objects.create_user(username="offline-user", password="test")
    client.force_login(user)
    session = OfflineSession.objects.create(
        kind=OfflineSession.Kind.UNPLANNED,
        status=OfflineSession.Status.ACTIVE,
        local_hostname="warehouse-pc",
        instance_id="warehouse-pc",
        base_backup_run_id="run",
        base_backup_created_at=timezone.now(),
        base_manifest={},
        base_data_marker={},
        base_migration_fingerprint=MIGRATION_HASH,
    )
    state.state_reason = f"offline-session:{session.id}"
    state.save()

    response = client.get("/")

    assert response.status_code == 200
    body = response.content.decode("utf-8")
    assert "АВТОНОМНЫЙ РЕЖИМ" in body
    assert "Данные работают локально" in body
    assert "warehouse-pc" in body
    assert "Статус: Автономная работа" in body


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local", DENSTOCK_INSTANCE_ID="warehouse-pc")
def test_completed_history_is_displayed_as_standby_not_active_offline_session():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.NORMAL
    state.save()
    OfflineSession.objects.create(
        kind=OfflineSession.Kind.UNPLANNED,
        status=OfflineSession.Status.COMPLETED,
        local_hostname="warehouse-pc",
        instance_id="warehouse-pc",
        base_backup_run_id="run",
        base_backup_created_at=timezone.now(),
        base_manifest={},
        base_data_marker={},
        base_migration_fingerprint=MIGRATION_HASH,
    )

    context = emergency_context()

    assert context["standby_only"] is True
    assert context["session"] is None
