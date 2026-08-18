import uuid

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.operations.emergency_primary import (
    EmergencyPrimaryAuthorizationError,
    authorize_emergency_primary,
    revoke_emergency_primary,
)
from apps.operations.models import DeploymentState, EmergencyAuditEvent


def test_authorize_replace_revoke_is_auditable(db, settings):
    settings.DENSTOCK_MODE = "production"
    first, second = uuid.uuid4(), uuid.uuid4()
    state = authorize_emergency_primary(str(first), actor="release-admin")
    assert state.authorized_emergency_primary_id == first
    assert state.primary_authorization_epoch == 1
    same = authorize_emergency_primary(str(first), actor="release-admin")
    assert same.primary_authorization_epoch == 1
    state = authorize_emergency_primary(str(second), actor="release-admin")
    assert state.authorized_emergency_primary_id == second
    assert state.primary_authorization_epoch == 2
    state = revoke_emergency_primary(actor="release-admin")
    assert state.authorized_emergency_primary_id is None
    assert state.primary_authorization_epoch == 3
    assert EmergencyAuditEvent.objects.count() == 3


def test_authorization_is_production_only(db, settings):
    settings.DENSTOCK_MODE = "test"
    with pytest.raises(EmergencyPrimaryAuthorizationError):
        authorize_emergency_primary(str(uuid.uuid4()), actor="warehouse-user")
    assert DeploymentState.get_solo().authorized_emergency_primary_id is None


def test_authorize_and_revoke_commands_require_exact_confirmation(db, settings):
    settings.DENSTOCK_MODE = "production"
    workstation_id = uuid.uuid4()

    with pytest.raises(CommandError, match="точная фраза"):
        call_command(
            "authorize_emergency_primary",
            workstation_id=str(workstation_id),
            actor="release-admin",
            confirm="wrong",
        )
    call_command(
        "authorize_emergency_primary",
        workstation_id=str(workstation_id),
        actor="release-admin",
        confirm="НАЗНАЧИТЬ-EMERGENCY-PRIMARY",
    )
    assert DeploymentState.get_solo().authorized_emergency_primary_id == workstation_id

    with pytest.raises(CommandError, match="точная фраза"):
        call_command("revoke_emergency_primary", actor="release-admin", confirm="wrong")
    call_command(
        "revoke_emergency_primary",
        actor="release-admin",
        confirm="ОТОЗВАТЬ-EMERGENCY-PRIMARY",
    )
    assert DeploymentState.get_solo().authorized_emergency_primary_id is None
