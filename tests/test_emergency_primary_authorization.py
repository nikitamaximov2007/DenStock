import uuid

import pytest

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
