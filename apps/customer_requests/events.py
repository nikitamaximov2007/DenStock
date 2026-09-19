"""Emit compact, transactional events for the internal request workspace."""
from __future__ import annotations

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import (
    CustomerRequest,
    CustomerRequestStatusEvent,
    MaxMessage,
    TelegramMessage,
    WorkspaceEvent,
)


def _request_for(instance):
    if isinstance(instance, CustomerRequest):
        return instance
    conversation = getattr(instance, "conversation", None)
    return getattr(conversation, "request", None)


def _emit(*, event_type: str, instance, request=None, deleted=False):
    if request is None:
        request = _request_for(instance)
    if request is None:
        return
    request_id = getattr(request, "pk", None) or getattr(instance, "pk", None)
    entity_id = str(getattr(instance, "pk", ""))
    payload = {
        "request_id": request_id,
        "entity_type": instance.__class__.__name__,
        "entity_id": entity_id,
    }
    if request is not None and getattr(request, "human_number", None) is not None:
        payload["number"] = request.human_number
    if deleted:
        payload["deleted"] = True
    WorkspaceEvent.objects.create(
        event_type=event_type,
        request=request,
        entity_type=instance.__class__.__name__,
        entity_id=entity_id,
        payload=payload,
    )


@receiver(post_save, sender=CustomerRequest)
def request_saved(sender, instance, created, **kwargs):
    _emit(event_type="request_created" if created else "request_updated", instance=instance)


@receiver(post_save, sender=CustomerRequestStatusEvent)
def status_saved(sender, instance, created, **kwargs):
    if created:
        _emit(event_type="request_status_changed", instance=instance, request=instance.request)


@receiver(post_save, sender=TelegramMessage)
def telegram_message_saved(sender, instance, created, **kwargs):
    if created:
        kind = (
            "customer_message_created"
            if instance.direction == TelegramMessage.Direction.CUSTOMER
            else "operator_message_created"
        )
        _emit(event_type=kind, instance=instance)
    elif instance.direction == TelegramMessage.Direction.OPERATOR:
        _emit(event_type="operator_message_status_changed", instance=instance)


@receiver(post_save, sender=MaxMessage)
def max_message_saved(sender, instance, created, **kwargs):
    if created:
        kind = (
            "customer_message_created"
            if instance.direction == MaxMessage.Direction.CUSTOMER
            else "operator_message_created"
        )
        _emit(event_type=kind, instance=instance)
    elif instance.direction == MaxMessage.Direction.OPERATOR:
        _emit(event_type="operator_message_status_changed", instance=instance)


@receiver(post_delete, sender=TelegramMessage)
@receiver(post_delete, sender=MaxMessage)
def attachment_deleted(sender, instance, **kwargs):
    if instance.attachment:
        instance.attachment.delete(save=False)


@receiver(post_delete, sender=CustomerRequest)
def request_deleted(sender, instance, **kwargs):
    WorkspaceEvent.objects.create(
        event_type="request_deleted",
        request=None,
        entity_type="CustomerRequest",
        entity_id=str(instance.pk),
        payload={
            "request_id": instance.pk,
            "entity_type": "CustomerRequest",
            "entity_id": str(instance.pk),
            "deleted": True,
        },
    )
