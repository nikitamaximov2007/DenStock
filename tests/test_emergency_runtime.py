from datetime import timedelta

import pytest
from django.http import HttpResponse
from django.test import RequestFactory, override_settings
from django.utils import timezone

from apps.ai_support.services import FeatureDisabled, send_message
from apps.ai_support.views import _provider_state
from apps.operations.emergency_lifecycle import EmergencyLifecycleError, start_offline_session
from apps.operations.models import DeploymentState, OfflineSession
from apps.operations.write_guard import BusinessWriteBlocked, BusinessWriteGuardMiddleware
from apps.suppliers.models import Supplier

COMMIT = "a" * 40
MIGRATION_HASH = "b" * 64
DATA_HASH = "c" * 64
DATABASE_ID = "52347a14-d939-45e6-a397-06c79ef257f2"


def _base_manifest(*, consistency="database_snapshot"):
    return {
        "backup_run_id": "d7919779-6c24-43cb-bb78-181f61a335d5",
        "created_at": (timezone.now() - timedelta(hours=2)).isoformat(),
        "app_commit": COMMIT,
        "database_identity": DATABASE_ID,
        "migration_fingerprint": MIGRATION_HASH,
        "media_tree_sha256": "d" * 64,
        "data_state": {"business_sha256": DATA_HASH},
        "consistency": consistency,
    }


@pytest.fixture
def lifecycle_runtime(monkeypatch, settings):
    settings.DENSTOCK_MODE = "emergency-local"
    settings.DENSTOCK_INSTANCE_ID = "warehouse-pc-test"
    settings.DENSTOCK_APP_COMMIT = COMMIT
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
    return state


@pytest.mark.django_db(transaction=True)
def test_start_from_verified_standby_creates_active_session(lifecycle_runtime, monkeypatch):
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: ({"database_name": "ignored"}, _base_manifest()),
    )

    session = start_offline_session(kind=OfflineSession.Kind.UNPLANNED, actor="operator")

    lifecycle_runtime.refresh_from_db()
    assert session.status == OfflineSession.Status.ACTIVE
    assert session.base_data_marker == {"business_sha256": DATA_HASH}
    assert lifecycle_runtime.write_state == DeploymentState.WriteState.EMERGENCY_ACTIVE


@pytest.mark.django_db(transaction=True)
def test_two_start_commands_are_serialized_and_second_is_denied(
    lifecycle_runtime, monkeypatch
):
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: ({"database_name": "ignored"}, _base_manifest()),
    )
    start_offline_session(kind=OfflineSession.Kind.UNPLANNED)

    with pytest.raises(EmergencyLifecycleError, match="lifecycle"):
        start_offline_session(kind=OfflineSession.Kind.UNPLANNED)


@pytest.mark.django_db(transaction=True)
def test_planned_start_requires_maintenance_consistent_backup(
    lifecycle_runtime, monkeypatch
):
    monkeypatch.setattr(
        "apps.operations.emergency_lifecycle._active_standby",
        lambda paths=None: ({"database_name": "ignored"}, _base_manifest()),
    )

    with pytest.raises(EmergencyLifecycleError, match="maintenance lock"):
        start_offline_session(kind=OfflineSession.Kind.PLANNED)


@pytest.mark.django_db(transaction=True)
@override_settings(DENSTOCK_MODE="production")
def test_direct_business_write_is_blocked_in_maintenance():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.MAINTENANCE
    state.save()

    with pytest.raises(BusinessWriteBlocked):
        Supplier.objects.create(name="Blocked supplier")


@pytest.mark.django_db(transaction=True)
@override_settings(DENSTOCK_MODE="emergency-local")
def test_business_write_requires_active_local_session():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.NORMAL
    state.save()
    with pytest.raises(BusinessWriteBlocked):
        Supplier.objects.create(name="Standby write")

    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    supplier = Supplier.objects.create(name="Offline supplier")
    assert supplier.pk


@pytest.mark.django_db(transaction=True)
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


@pytest.mark.django_db(transaction=True)
@override_settings(DENSTOCK_MODE="production")
def test_http_write_returns_locked_when_production_is_frozen():
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.MAINTENANCE
    state.save()
    middleware = BusinessWriteGuardMiddleware(lambda request: HttpResponse("unexpected"))

    response = middleware(RequestFactory().post("/suppliers/new/"))

    assert response.status_code == 423


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local", AI_SUPPORT_ENABLED=True)
def test_ai_support_is_gracefully_unavailable_offline():
    assert _provider_state() == "offline"
    with pytest.raises(FeatureDisabled):
        send_message(conversation=None, user=None, text="question", token=None)


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
