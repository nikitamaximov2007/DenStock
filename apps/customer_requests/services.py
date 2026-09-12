"""Business rules for requests. This module never reserves or moves stock."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.catalog.models import PartType
from apps.catalog.public_contracts import resolve_current_customer_price
from apps.core.phones import canonical_phone_text, normalize_phone
from apps.inventory.availability import available_totals
from apps.inventory.presentation import part_exact_number, with_part_identity

from .models import (
    CustomerRequest,
    CustomerRequestLine,
    CustomerRequestPrivacyEvent,
    CustomerRequestStatusEvent,
)
from .policies import PUBLIC_REQUEST_CONSENT_PURPOSE

ZERO = Decimal("0")
MAX_REQUEST_LINES = 50


class CustomerRequestError(ValueError):
    """A customer-facing validation error without sensitive details.

    ``field`` names the form field at fault, when there is one, so a form can
    mark that field and point it at the message.
    """

    def __init__(self, message: str = "", *, field: str | None = None):
        super().__init__(message)
        self.field = field


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


def _required_text(value, field, maximum, *, form_field=None):
    cleaned = str(value or "").strip()
    if not cleaned:
        raise CustomerRequestError(f"Укажите {field}.", field=form_field)
    if len(cleaned) > maximum:
        raise CustomerRequestError(f"Поле «{field}» слишком длинное.", field=form_field)
    return cleaned


def _phone(value: str) -> str:
    """Телефон заявки: проверка и единая запись номера.

    Решает сервер, а не браузер: маска в форме делает то же самое, но заявка,
    отправленная с выключенным JS или из чужого клиента, получает ровно такую
    же каноническую запись.
    """
    value = _required_text(value, "телефон", 50, form_field="customer_phone")
    normalized = normalize_phone(value)
    if len(normalized) < 5 or any(char.isalpha() for char in value):
        raise CustomerRequestError("Укажите телефон в обычном формате.", field="customer_phone")
    return canonical_phone_text(value)


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


def _quantity_text(value: Decimal) -> str:
    return format(value.normalize(), "f").replace(".", ",") if value else "0"


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
    # The public database role may read only these columns of a request.
    existing = (
        CustomerRequest.objects.filter(submission_key_hash=key_hash)
        .only("pk", "public_id")
        .order_by("pk")
        .first()
    )
    if existing:
        return existing, False

    customer_name = _required_text(customer_name, "имя", 255, form_field="customer_name")
    customer_phone = _phone(customer_phone)
    if preferred_messenger not in CustomerRequest.Messenger.values:
        raise CustomerRequestError("Выберите Telegram или MAX.", field="preferred_messenger")
    comment = str(comment or "").strip()
    if len(comment) > 2000:
        raise CustomerRequestError(
            "Комментарий не должен быть длиннее 2000 символов.", field="comment"
        )
    privacy_policy_version = _required_text(privacy_policy_version, "версию политики", 64)
    personal_data_consent_version = _required_text(
        personal_data_consent_version, "версию согласия", 64
    )
    consent_purpose = _required_text(consent_purpose, "цель согласия", 120)
    line_inputs = _validated_lines(lines)
    part_ids = [line.part_id for line in line_inputs]
    candidates = PartType.objects.filter(pk__in=part_ids, is_active=True)
    if source == CustomerRequest.Source.PUBLIC_CATALOG:
        # A public request can name only a part the public catalog shows.
        candidates = candidates.filter(is_public=True)
    parts = {
        part.pk: part
        for part in with_part_identity(candidates.select_related("unit"), part_field="")
    }
    if len(parts) != len(part_ids):
        raise CustomerRequestError("Одна или несколько деталей больше недоступны.")
    availability = available_totals(part_ids)
    prepared_lines = []
    for line in line_inputs:
        part = parts[line.part_id]
        current_available = availability[line.part_id]
        label = part_exact_number(part, default="") or part.name
        if line.supply_inquiry:
            if current_available > ZERO:
                raise CustomerRequestError(
                    f"{label}: деталь появилась в наличии. Запрос о поставке доступен"
                    " только для детали без остатка."
                )
        elif line.quantity > current_available:
            raise CustomerRequestError(
                f"{label}: Сейчас доступно {_quantity_text(current_available)}."
                " Измените количество или запросите поставку."
            )
        price = resolve_current_customer_price(part).price_rub
        prepared_lines.append((line, part, price))

    try:
        # Keep the outer transaction usable after a duplicate-key race.  The
        # savepoint is essential on PostgreSQL: querying after an IntegrityError
        # raised directly in the outer atomic block would be forbidden.
        with transaction.atomic():
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
        return (
            CustomerRequest.objects.only("pk", "public_id").get(submission_key_hash=key_hash),
            False,
        )

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


@transaction.atomic
def withdraw_consent(*, request_id: int, by=None) -> CustomerRequest:
    """Record withdrawal without silently deleting the operational record."""
    request = CustomerRequest.objects.select_for_update().get(pk=request_id)
    if request.consent_withdrawn_at is None:
        request.consent_withdrawn_at = timezone.now()
        request.save(update_fields=["consent_withdrawn_at", "updated_at"])
        CustomerRequestPrivacyEvent.objects.create(
            request=request,
            event_type=CustomerRequestPrivacyEvent.EventType.CONSENT_WITHDRAWN,
            performed_by=by,
        )
    return request


@transaction.atomic
def anonymize_request(*, request_id: int, by=None) -> CustomerRequest:
    """Explicit irreversible PII minimization after a reviewed withdrawal."""
    request = CustomerRequest.objects.select_for_update().get(pk=request_id)
    if request.consent_withdrawn_at is None:
        raise CustomerRequestError("Сначала зафиксируйте отзыв согласия.")
    if request.data_anonymized_at is not None:
        return request
    request.customer_name = ""
    request.customer_phone = ""
    request.comment = ""
    request.data_anonymized_at = timezone.now()
    request.save(
        update_fields=[
            "customer_name",
            "customer_phone",
            "customer_phone_normalized",
            "comment",
            "data_anonymized_at",
            "updated_at",
        ]
    )
    CustomerRequestPrivacyEvent.objects.create(
        request=request,
        event_type=CustomerRequestPrivacyEvent.EventType.ANONYMIZED,
        performed_by=by,
    )
    return request
