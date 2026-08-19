"""Explicit production-side authorization of the single emergency primary."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import transaction

from .emergency_state import record_event
from .models import DeploymentState


class EmergencyPrimaryAuthorizationError(RuntimeError):
    pass


@transaction.atomic
def authorize_emergency_primary(workstation_id: str, *, actor: str) -> DeploymentState:
    """Atomically replace the designated primary. Caller must be production admin."""
    if settings.DENSTOCK_MODE != "production":
        raise EmergencyPrimaryAuthorizationError("Primary назначается только в production.")
    try:
        identity = uuid.UUID(str(workstation_id))
    except (TypeError, ValueError):
        raise EmergencyPrimaryAuthorizationError("workstation_id должен быть UUID.") from None
    state = DeploymentState.objects.select_for_update().get(pk=DeploymentState.SINGLETON_PK)
    if state.authorized_emergency_primary_id == identity:
        return state
    previous = state.authorized_emergency_primary_id
    state.authorized_emergency_primary_id = identity
    state.primary_authorization_epoch += 1
    state.save(
        update_fields=[
            "authorized_emergency_primary_id",
            "primary_authorization_epoch",
            "updated_at",
        ]
    )
    record_event(
        "emergency_primary_authorized",
        "success",
        actor=actor,
        details={
            "old_workstation_id": str(previous) if previous else None,
            "new_workstation_id": str(identity),
            "epoch": state.primary_authorization_epoch,
        },
    )
    return state


@transaction.atomic
def revoke_emergency_primary(*, actor: str) -> DeploymentState:
    if settings.DENSTOCK_MODE != "production":
        raise EmergencyPrimaryAuthorizationError("Primary отзывается только в production.")
    state = DeploymentState.objects.select_for_update().get(pk=DeploymentState.SINGLETON_PK)
    previous = state.authorized_emergency_primary_id
    if previous is None:
        return state
    state.authorized_emergency_primary_id = None
    state.primary_authorization_epoch += 1
    state.save(
        update_fields=[
            "authorized_emergency_primary_id",
            "primary_authorization_epoch",
            "updated_at",
        ]
    )
    record_event(
        "emergency_primary_revoked",
        "success",
        actor=actor,
        details={
            "old_workstation_id": str(previous),
            "new_workstation_id": None,
            "epoch": state.primary_authorization_epoch,
        },
    )
    return state
