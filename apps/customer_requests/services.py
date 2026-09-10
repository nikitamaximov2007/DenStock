"""Business rules for requests. This module never reserves or moves stock."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import PartType
from apps.catalog.public_contracts import resolve_current_customer_price
from apps.core.phones import normalize_phone
from apps.inventory.availability import available_totals
from apps.inventory.presentation import part_exact_number, with_part_identity

from .models import CustomerRequest, CustomerRequestLine, CustomerRequestStatusEvent
from .policies import PUBLIC_REQUEST_CONSENT_PURPOSE

ZERO = Decimal("0")
MAX_REQUEST_LINES = 50


class CustomerRequestError(ValueError):
    """A customer-facing validation error without sensitive details."""


@dataclass(frozen=True, slots=True)
class RequestLineInput:
    part_id: int
    quantity: Decimal | str | int
    supply_inquiry: bool = False


def submission_key_hash(value: str) -> str:
    value = str(value or "").strip()
    if not 16 <= len(value) <= 200:
        raise CustomerRequestError("Некорректный ключ отправки заявки.")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _required_text(value, field, maximum):
    cleaned = str(value or "").strip()
    if not cleaned:
        raise CustomerRequestError(f"Укажите {field}.")
    if len(cleaned) > maximum:
        raise CustomerRequestError(f"Поле «{field}» слишком длинное.")
    return cleaned


def _phone(value: str) -> str:
    value = _required_text(value, "телефон", 50)
    normalized = normalize_phone(value)
    if len(normalized) < 5 or any(char.isalpha() for char in value):
        raise CustomerRequestError("Укажите телефон в обычном формате.")
    return value


def _quantity(value) -> Decimal:
    try:
        quantity = Decimal(str(value).strip().replace(",", "."))
    except (InvalidOperation, ValueError) as exc:
        raise CustomerRequestError("Количество должно быть числом.") from exc
    if not quantity.is_finite() or quantity <= ZERO or quantity.as_tuple().exponent < -3:
        raise CustomerRequestError("Укажите положительное количество с точностью до 0,001.")
    if quantity >= Decimal("1000000000"):
        raise CustomerRequestError("Указано слишком большое количество.")
    return quantity


def _validated_lines(lines) -> list[RequestLineInput]:
    values = list(lines or [])
    if not values:
        raise CustomerRequestError("Добавьте хотя бы одну деталь.")
    if len(values) > MAX_REQUEST_LINES:
        raise CustomerRequestError("В одной заявке может быть не более 50 позиций.")
    result = []
    part_ids = set()
    for raw in values:
        line = raw if isinstance(raw, RequestLineInput) else RequestLineInput(**raw)
        try:
            part_id = int(line.part_id)
        except (TypeError, ValueError) as exc:
            raise CustomerRequestError("Деталь не найдена.") from exc
        if part_id <= 0 or part_id in part_ids:
            raise CustomerRequestError("Каждая деталь должна быть в заявке только один раз.")
        part_ids.add(part_id)
        result.append(
            RequestLineInput(part_id, _quantity(line.quantity), bool(line.supply_inquiry))
        )
    return result


def _validate_status_transition(current: str, target: str) -> None:
    allowed = {
        CustomerRequest.Status.NEW: {
            CustomerRequest.Status.IN_PROGRESS,
            CustomerRequest.Status.CANCELED,
        },
        CustomerRequest.Status.IN_PROGRESS: {
            CustomerRequest.Status.COMPLETED,
            CustomerRequest.Status.CANCELED,
        },
    }
    if target not in allowed.get(current, set()):
        raise CustomerRequestError("Этот переход статуса недоступен.")


@transaction.atomic
def create_customer_request(
    *,
    customer_name: str,
    customer_phone: str,
    preferred_messenger: str,
    comment: str = "",
    lines,
    privacy_policy_version: str,
    personal_data_consent_version: str,
    submission_key: str,
    source: str = CustomerRequest.Source.PUBLIC_CATALOG,
    consent_purpose: str = PUBLIC_REQUEST_CONSENT_PURPOSE,
) -> tuple[CustomerRequest, bool]:
    """Create a request and immutable line snapshots, without stock mutation.

    A repeated anonymous submission key returns the original request. The
    unique hash is also safe when two browser retries reach separate workers.
    """
    key_hash = submission_key_hash(submission_key)
    existing = CustomerRequest.objects.filter(submission_key_hash=key_hash).first()
    if existing:
        return existing, False

    customer_name = _required_text(customer_name, "имя", 255)
    customer_phone = _phone(customer_phone)
    if preferred_messenger not in CustomerRequest.Messenger.values:
        raise CustomerRequestError("Выберите Telegram или MAX.")
    comment = str(comment or "").strip()
    if len(comment) > 2000:
        raise CustomerRequestError("Комментарий не должен быть длиннее 2000 символов.")
    privacy_policy_version = _required_text(privacy_policy_version, "версию политики", 64)
    personal_data_consent_version = _required_text(
        personal_data_consent_version, "версию согласия", 64
    )
    consent_purpose = _required_text(consent_purpose, "цель согласия", 120)
    line_inputs = _validated_lines(lines)
    part_ids = [line.part_id for line in line_inputs]
    parts = {
        part.pk: part
        for part in with_part_identity(
            PartType.objects.filter(pk__in=part_ids, is_active=True).select_related("unit"),
            part_field="",
        )
    }
    if len(parts) != len(part_ids):
        raise CustomerRequestError("Одна или несколько деталей больше недоступны.")
    availability = available_totals(part_ids)
    prepared_lines = []
    for line in line_inputs:
        part = parts[line.part_id]
        current_available = availability[line.part_id]
        if line.supply_inquiry:
            if current_available > ZERO:
                raise CustomerRequestError(
                    "Запрос о поставке доступен только для детали без остатка."
                )
        elif line.quantity > current_available:
            raise CustomerRequestError(
                f"Сейчас доступно: {current_available}. Измените количество или запросите поставку."
            )
        price = resolve_current_customer_price(part).price_rub
        prepared_lines.append((line, part, price))

    try:
        request = CustomerRequest.objects.create(
            source=source,
            customer_name=customer_name,
            customer_phone=customer_phone,
            preferred_messenger=preferred_messenger,
            comment=comment,
            privacy_policy_version=privacy_policy_version,
            personal_data_consent_version=personal_data_consent_version,
            consent_purpose=consent_purpose,
            consent_accepted_at=timezone.now(),
            submission_key_hash=key_hash,
        )
    except IntegrityError:
        # A concurrent retry won the unique key race. It is the same logical
        # submission, not a second request.
        return CustomerRequest.objects.get(submission_key_hash=key_hash), False

    CustomerRequestLine.objects.bulk_create(
        [
            CustomerRequestLine(
                request=request,
                part_type=part,
                quantity_requested=line.quantity,
                unit_name=part.unit.name,
                unit_short_name=part.unit.short_name,
                price_seen=price,
                article=part_exact_number(part, default=""),
                part_name=part.name,
                is_supply_inquiry=line.supply_inquiry,
            )
            for line, part, price in prepared_lines
        ]
    )
    return request, True


@transaction.atomic
def change_request_status(
    *, request_id: int, target_status: str, by
) -> tuple[CustomerRequest, bool]:
    """Apply one permitted transition; retrying the same POST is idempotent."""
    request = CustomerRequest.objects.select_for_update().get(pk=request_id)
    if target_status == request.status:
        return request, False
    _validate_status_transition(request.status, target_status)
    previous = request.status
    request.status = target_status
    request.save(update_fields=["status", "updated_at"])
    CustomerRequestStatusEvent.objects.create(
        request=request, from_status=previous, to_status=target_status, changed_by=by
    )
    return request, True


def withdraw_consent(*, request_id: int, by=None) -> CustomerRequest:
    """Record withdrawal; anonymization remains an explicit reviewed operation."""
    request = CustomerRequest.objects.get(pk=request_id)
    if request.consent_withdrawn_at is None:
        request.consent_withdrawn_at = timezone.now()
        request.save(update_fields=["consent_withdrawn_at", "updated_at"])
    return request
