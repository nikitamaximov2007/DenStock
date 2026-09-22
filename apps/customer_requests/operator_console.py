"""Opt-in mobile operator console for explicitly paired staff identities."""
from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from apps.catalog.models import PartType, PartTypeImage
from apps.catalog.photo_pipeline import PartPhotoAlreadyExists, upload_primary_part_photo
from apps.catalog.public_photos import PublicPhotoError
from apps.core.files import validate_image_upload
from apps.inventory.presentation import part_exact_number
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.sales.models import Sale, SaleLine

from . import operator_replies, workspace
from .attachments import AttachmentError, ValidatedAttachment
from .models import (
    CustomerRequest,
    MaxMessage,
    OperatorConsoleRuntime,
    OperatorConversationContext,
    OperatorNotification,
    OwnerPhotoUploadContext,
    OwnerPhotoUploadReceipt,
    StaffMessengerBinding,
    StaffMessengerPairingToken,
    TelegramMessage,
)

LIST_PAGE_SIZE = 8
PHOTO_OPERATION_PAGE_SIZE = 6
PAIRING_TTL = timedelta(minutes=10)
PAIRING_CODE_RE = re.compile(r"^[A-Z0-9]{4}(?:-[A-Z0-9]{4}){2}$")
PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
OWNER_OPERATOR_KEYS = {"Денис": "DENIS", "Рим": "RIM"}


def enabled() -> bool:
    return bool(settings.CUSTOMER_OPERATOR_CONSOLE_ENABLED)


def _valid_provider(value: str) -> str | None:
    return value if value in StaffMessengerBinding.Provider.values else None


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def binding_for(provider: str, provider_user_id: int, *, lock: bool = False):
    """Re-read provider identity and internal permission for every action."""
    provider = _valid_provider(provider)
    if provider is None or not isinstance(provider_user_id, int) or provider_user_id <= 0:
        return None
    query = StaffMessengerBinding.objects.select_related("user").filter(
        provider=provider,
        provider_user_id=provider_user_id,
        is_active=True,
        user__is_active=True,
    )
    if lock:
        query = query.select_for_update()
    binding = query.first()
    return binding if binding and binding.user.can_manage_sales else None


def _new_pairing_code() -> str:
    raw = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(12))
    return "-".join(raw[index : index + 4] for index in range(0, 12, 4))


def is_pairing_code(value: str) -> bool:
    return bool(PAIRING_CODE_RE.fullmatch((value or "").strip().upper()))


def _operator_key(*, label: str, operator_key: str | None) -> str:
    if operator_key:
        value = operator_key.strip().upper()
    else:
        value = OWNER_OPERATOR_KEYS.get(label) or f"LABEL_{_hash(label)[:16].upper()}"
    if not value or len(value) > 32 or not re.fullmatch(r"[A-Z0-9_]+", value):
        raise ValueError("Укажите корректную личность оператора.")
    return value


def issue_pairing_token(
    *, user, provider: str | None = None, label: str, created_by, operator_key: str | None = None
) -> str:
    # ``provider`` remains accepted for callers from the old admin UI, but a
    # newly issued code is deliberately provider-neutral.
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise ValueError("Укажите подпись сотрудника длиной до 80 символов.")
    operator_key = _operator_key(label=label, operator_key=operator_key)
    raw = _new_pairing_code()
    with transaction.atomic():
        StaffMessengerPairingToken.objects.filter(
            user=user, operator_key=operator_key, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        StaffMessengerPairingToken.objects.create(
            token_hash=_hash(raw),
            user=user,
            operator_key=operator_key,
            provider="",
            customer_visible_label=label,
            expires_at=timezone.now() + PAIRING_TTL,
            created_by=created_by,
        )
    return raw


@transaction.atomic
def consume_pairing(
    *, provider: str, provider_user_id: int, raw_token: str, provider_chat_id: int | None = None
):
    if provider not in StaffMessengerPairingToken.Provider.values:
        return None, None
    token = (raw_token or "").strip()
    if not is_pairing_code(token):
        return None, None
    row = (
        StaffMessengerPairingToken.objects.select_for_update()
        .select_related("user")
        .filter(
            token_hash=_hash(token),
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if (
        row is None
        or row.provider
        or not row.user.is_active
        or not row.user.can_manage_sales
        or (provider == StaffMessengerBinding.Provider.TELEGRAM and row.telegram_consumed_at)
        or (provider == StaffMessengerBinding.Provider.MAX and row.max_consumed_at)
    ):
        return None, None
    existing = StaffMessengerBinding.objects.filter(
        provider=provider, provider_user_id=provider_user_id
    ).first()
    if existing and existing.is_active:
        return None, None
    if existing and (
        existing.user_id != row.user_id or existing.operator_key != row.operator_key
    ):
        return None, None
    if existing is None and StaffMessengerBinding.objects.filter(
        user=row.user, provider=provider, operator_key=row.operator_key
    ).exists():
        return None, None
    if existing is not None:
        binding = existing
        binding.is_active = True
        binding.operator_mode = False
        binding.operator_key = row.operator_key
        clear_context(binding=binding)
        clear_photo_context(binding=binding)
        binding.customer_visible_label = row.customer_visible_label
        binding.created_by = row.created_by
        if provider == StaffMessengerBinding.Provider.MAX and isinstance(provider_chat_id, int):
            binding.delivery_chat_id = provider_chat_id
        binding.save()
    else:
        binding = StaffMessengerBinding.objects.create(
            user=row.user,
            operator_key=row.operator_key,
            provider=provider,
            provider_user_id=provider_user_id,
            delivery_chat_id=(
                provider_chat_id if provider == StaffMessengerBinding.Provider.MAX else None
            ),
            customer_visible_label=row.customer_visible_label,
            created_by=row.created_by,
        )
    now = timezone.now()
    slot = (
        "telegram_consumed_at"
        if provider == StaffMessengerBinding.Provider.TELEGRAM
        else "max_consumed_at"
    )
    setattr(row, slot, now)
    row.save(update_fields=[slot])
    clear_panel_delivery(binding=binding)
    if enabled():
        return binding, (
            "Доступ владельца подключён.\n\n"
            f"Вы вошли как: {binding.customer_visible_label}"
        )
    return binding, (
        "Доступ владельца подключён.\n\n"
        f"Вы вошли как: {binding.customer_visible_label}\n\n"
        "Рабочая панель пока не активирована."
    )


def _context_is_fresh(context) -> bool:
    ttl = max(1, int(getattr(settings, "CUSTOMER_OPERATOR_CONTEXT_TTL_MINUTES", 30)))
    return context.updated_at >= timezone.now() - timedelta(minutes=ttl)


def _photo_context_ttl() -> timedelta:
    ttl = max(1, int(getattr(settings, "CUSTOMER_OPERATOR_PHOTO_CONTEXT_TTL_MINUTES", 5)))
    return timedelta(minutes=ttl)


def _session_token(binding) -> str:
    """Opaque token for the current binding session."""
    context, _ = OperatorConversationContext.objects.get_or_create(binding=binding)
    return _hash(f"operator-session:{binding.pk}:{context.updated_at.isoformat()}")[:16]


def _callback(binding, kind: str, value: str = "") -> str:
    token = _session_token(binding)
    suffix = f":{value}" if value else ""
    return f"op:{kind}:{token}{suffix}"


def menu(binding=None) -> tuple[str, dict]:
    heading = "Панель владельца PRO-STORE"
    if binding is not None:
        context = OperatorConversationContext.objects.select_related("request").filter(
            binding=binding
        ).first()
        if context and context.request_id and _context_is_fresh(context):
            request = context.request
            heading += (
                f"\nСейчас открыт диалог: №{request.reference} - "
                f"{(request.customer_name or 'Клиент')[:80]}"
            )
    return heading, {"inline_keyboard": [
        [{"text": "Все заявки", "callback_data": _callback(binding, "l", "1")}],
        [{"text": "Новые заявки", "callback_data": _callback(binding, "n", "1")}],
        *(
            [[{
                "text": "Загрузка фото по продажам/ремонтам",
                "callback_data": _callback(binding, "p", "1"),
            }]]
            if binding is not None
            and binding.provider == StaffMessengerBinding.Provider.TELEGRAM
            else []
        ),
    ]}


def buttons_for_provider(markup: dict | None, provider: str) -> dict | None:
    """Translate shared callback markup to the provider's button shape."""
    if markup is None or provider != StaffMessengerBinding.Provider.MAX:
        return markup
    return {
        "inline_keyboard": [
            [
                {"text": button["text"], "payload": button["callback_data"]}
                for button in row
                if isinstance(button, dict) and "callback_data" in button
            ]
            for row in markup.get("inline_keyboard", [])
        ]
    }


def _query(*, new_only: bool):
    query = CustomerRequest.objects.all()
    if new_only:
        query = query.filter(status=CustomerRequest.Status.NEW)
    else:
        query = query.filter(status__in=workspace.OPEN_STATUSES)
    return workspace.order_by_priority(workspace.annotate_workspace(query))


def request_page(page: int = 1, *, new_only: bool = False, binding=None) -> tuple[str, dict]:
    query = _query(new_only=new_only)
    total = query.count()
    pages = max(1, (total + LIST_PAGE_SIZE - 1) // LIST_PAGE_SIZE)
    page = min(max(1, int(page or 1)), pages)
    rows = []
    for request in query[(page - 1) * LIST_PAGE_SIZE : page * LIST_PAGE_SIZE]:
        rows.append([{"text": f"№{request.reference} - {(request.customer_name or 'Клиент')[:80]}",
                      "callback_data": _callback(binding, "c", request.public_id.hex)}])
    if not rows:
        return ("Новых заявок нет." if new_only else "Заявок нет."), menu(binding)[1]
    if page > 1 or page < pages:
        rows.append([
            *(
                [{"text": "Назад", "callback_data": _callback(
                    binding, "n" if new_only else "l", str(page - 1)
                )}]
                if page > 1
                else []
            ),
            *(
                [{"text": "Дальше", "callback_data": _callback(
                    binding, "n" if new_only else "l", str(page + 1)
                )}]
                if page < pages
                else []
            ),
        ])
    heading = "Новые заявки" if new_only else "Все заявки"
    return (f"{heading} · страница {page} из {pages}" if pages > 1 else heading,
            {"inline_keyboard": rows})


def request_by_hex(value: str):
    if not isinstance(value, str) or len(value) != 32:
        return None
    try:
        return CustomerRequest.objects.prefetch_related("lines").filter(public_id=value).first()
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class _PhotoOperation:
    kind: str
    pk: int
    when: object
    customer_name: str


def _photo_operations() -> list[_PhotoOperation]:
    sales = [
        _PhotoOperation("sale", sale.pk, sale.sold_at or sale.created_at, sale.customer_name)
        for sale in Sale.objects.filter(status=Sale.Status.COMPLETED)
    ]
    repairs = [
        _PhotoOperation(
            "repair", repair.pk, repair.completed_at or repair.created_at, repair.customer_name
        )
        for repair in RepairOrder.objects.filter(status=RepairOrder.Status.COMPLETED)
    ]
    # Newest first, then a stable kind and primary-key tie-breaker.  The
    # operation timestamp is the completed/sold timestamp, not row creation.
    return sorted(
        sales + repairs,
        key=lambda item: (item.when, item.kind, item.pk),
        reverse=True,
    )


def _photo_operation(ref: str):
    if not isinstance(ref, str) or ref.count(":") != 1:
        return None
    kind, value = ref.split(":", 1)
    if kind not in {"sale", "repair"} or not value.isdigit() or len(value) > 12:
        return None
    model = Sale if kind == "sale" else RepairOrder
    status = model.Status.COMPLETED
    return model.objects.filter(pk=int(value), status=status).first()


def _photo_operation_lines(operation, kind: str):
    line_model = SaleLine if kind == "sale" else RepairIssueLine
    field = "sale_id" if kind == "sale" else "repair_order_id"
    lines = line_model.objects.filter(**{field: operation.pk}).select_related("part_type")
    unique = {}
    for line in lines:
        unique.setdefault(line.part_type_id, line.part_type)
    return [unique[part_id] for part_id in sorted(unique)]


def photo_operation_page(page: int = 1, *, binding=None) -> tuple[str, dict]:
    operations = _photo_operations()
    pages = max(1, (len(operations) + PHOTO_OPERATION_PAGE_SIZE - 1) // PHOTO_OPERATION_PAGE_SIZE)
    page = min(max(1, int(page or 1)), pages)
    current = operations[(page - 1) * PHOTO_OPERATION_PAGE_SIZE : page * PHOTO_OPERATION_PAGE_SIZE]
    rows = []
    labels = {"sale": "ПРОДАЖА", "repair": "РЕМОНТ"}
    for operation in current:
        stamp = timezone.localtime(operation.when).strftime("%d.%m.%Y %H:%M:%S")
        rows.append([{
            "text": f"{stamp} {labels[operation.kind]}\n{operation.customer_name or 'Клиент'}",
            "callback_data": _callback(binding, "o", f"{operation.kind}-{operation.pk}"),
        }])
    if not rows:
        return "Продаж и ремонтов нет.", menu(binding)[1]
    navigation = []
    if page > 1:
        navigation.append({
            "text": "Назад", "callback_data": _callback(binding, "p", str(page - 1))
        })
    if page < pages:
        navigation.append({
            "text": "Далее", "callback_data": _callback(binding, "p", str(page + 1))
        })
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "В меню", "callback_data": _callback(binding, "m")}])
    heading = "Продажи и ремонты для загрузки фото"
    if pages > 1:
        heading += f" · страница {page} из {pages}"
    return heading, {"inline_keyboard": rows}


def _photo_operation_markup(binding, ref: str):
    return {"inline_keyboard": [
        [{"text": "К продажам и ремонтам", "callback_data": _callback(binding, "p", "1")}],
        [{"text": "В меню", "callback_data": _callback(binding, "m")}],
    ]}


def photo_operation_card(*, binding, kind: str, operation_id: int) -> tuple[str, dict]:
    operation = _photo_operation(f"{kind}:{operation_id}")
    if operation is None:
        return "Операция не найдена или ещё не проведена.", photo_operation_page(binding=binding)[1]
    labels = {"sale": "ПРОДАЖА", "repair": "РЕМОНТ"}
    operation_when = operation.sold_at if kind == "sale" else operation.completed_at
    stamp = timezone.localtime(operation_when or operation.created_at)
    lines = [
        f"{labels[kind]} {stamp:%d.%m.%Y %H:%M:%S}",
        operation.customer_name or "Клиент",
        "",
    ]
    rows = []
    for part in _photo_operation_lines(operation, kind):
        article = part_exact_number(part, default="Артикул не указан")
        rows.append([{
            "text": f"{article} {part.name}",
            "callback_data": _callback(binding, "q", f"{kind}-{operation_id}-{part.pk}"),
        }])
    if not rows:
        lines.append("Позиций детали нет.")
    rows.append([{"text": "К продажам и ремонтам", "callback_data": _callback(binding, "p", "1")}])
    return "\n".join(lines), {"inline_keyboard": rows}


def _photo_context(binding):
    context = OwnerPhotoUploadContext.objects.select_related("part_type").filter(
        binding=binding
    ).first()
    if context is not None and context.expires_at <= timezone.now():
        context.delete()
        return None
    return context


def clear_photo_context(*, binding) -> None:
    OwnerPhotoUploadContext.objects.filter(binding=binding).delete()


@transaction.atomic
def _photo_selection(*, binding, kind: str, operation_id: int, part_id: int):
    binding = StaffMessengerBinding.objects.select_for_update().get(pk=binding.pk)
    operation = _photo_operation(f"{kind}:{operation_id}")
    part_ids = {part.pk for part in _photo_operation_lines(operation, kind)} if operation else set()
    if operation is None or part_id not in part_ids:
        return "Позиция операции не найдена.", photo_operation_page(binding=binding)[1]
    part = PartType.objects.get(pk=part_id)
    article = part_exact_number(part, default="Артикул не указан")
    if PartTypeImage.objects.filter(part_id=part_id, is_active=True).exists():
        return (
            f"Фото уже загружено.\n\nАртикул: {article}\n{part.name}",
            _photo_operation_markup(binding, f"{kind}:{operation_id}"),
        )
    OwnerPhotoUploadContext.objects.update_or_create(
        binding=binding,
        defaults={
            "part_type": part,
            "operation_type": kind,
            "operation_id": operation_id,
            "article_snapshot": article,
            "part_name_snapshot": part.name,
            "expires_at": timezone.now() + _photo_context_ttl(),
        },
    )
    return (
        f"Фото отсутствует.\n\nОтправьте фотографию детали.\n"
        f"Она автоматически загрузится для артикула {article}\n"
        "в систему склада и каталог PRO-STOR.",
        {"inline_keyboard": [
            [{"text": "Отмена", "callback_data": _callback(binding, "x")}],
            [{"text": "Назад", "callback_data": _callback(binding, "o", f"{kind}-{operation_id}")}],
        ]},
    )


def _photo_upload_file(attachment):
    if isinstance(attachment, ValidatedAttachment):
        if not attachment.content_type.startswith("image/"):
            raise AttachmentError("Для этого действия отправьте изображение, а не PDF.")
        return ContentFile(attachment.content, name=attachment.filename)
    if attachment is None:
        raise AttachmentError("Фото не выбрано.")
    validate_image_upload(attachment)
    attachment.seek(0)
    return ContentFile(attachment.read(), name=getattr(attachment, "name", "photo.jpg"))


@transaction.atomic
def _consume_photo_upload(*, binding, external_id: str, attachment):
    binding = StaffMessengerBinding.objects.select_for_update().get(pk=binding.pk)
    existing = OwnerPhotoUploadReceipt.objects.filter(
        binding=binding, external_id=str(external_id)
    ).first()
    if existing is not None:
        return existing.response_text, menu(binding)[1]
    context = OwnerPhotoUploadContext.objects.select_for_update().select_related(
        "part_type"
    ).filter(binding=binding).first()
    if context is None or context.expires_at <= timezone.now():
        if context is not None:
            context.delete()
        return "Сначала выберите деталь в разделе загрузки фото.", menu(binding)[1]
    try:
        upload = _photo_upload_file(attachment)
        result = upload_primary_part_photo(
            part=context.part_type,
            upload=upload,
            source="telegram",
            owner_operator_key=binding.operator_key,
            operation_type=context.operation_type,
            operation_id=context.operation_id,
        )
    except (AttachmentError, ValidationError, PublicPhotoError) as exc:
        return str(exc), {
            "inline_keyboard": [[{"text": "Отмена", "callback_data": _callback(binding, "x")}]]
        }
    except PartPhotoAlreadyExists:
        text = "Для этой детали фото уже было загружено."
        clear_photo_context(binding=binding)
        OwnerPhotoUploadReceipt.objects.create(
            binding=binding, external_id=str(external_id), part_type=context.part_type,
            response_text=text,
        )
        return text, _photo_operation_markup(
            binding, f"{context.operation_type}-{context.operation_id}"
        )
    text = (
        f"Фото загружено.\n\nАртикул: {context.article_snapshot}\n"
        f"{context.part_name_snapshot}\n\nФото уже доступно в системе склада и каталоге PRO-STOR."
    )
    clear_photo_context(binding=binding)
    OwnerPhotoUploadReceipt.objects.create(
        binding=binding, external_id=str(external_id), part_type=result.image.part,
        response_text=text,
    )
    return text, _photo_operation_markup(
        binding, f"{context.operation_type}-{context.operation_id}"
    )


def set_context(*, binding, request_id: int):
    binding = binding_for(binding.provider, binding.provider_user_id, lock=True)
    if binding is None:
        return None, "Доступ отозван."
    request = CustomerRequest.objects.select_for_update().filter(pk=request_id).first()
    if request is None:
        return None, "Заявка не найдена."
    context, _ = OperatorConversationContext.objects.select_for_update().get_or_create(
        binding=binding
    )
    context.request = request
    context.save(update_fields=["request", "updated_at"])
    if not binding.operator_mode:
        binding.operator_mode = True
        binding.save(update_fields=["operator_mode", "updated_at"])
    return request, ""


def current_request(binding):
    context = (
        OperatorConversationContext.objects.select_related("request").filter(binding=binding).first()
    )
    if not context or not context.request_id:
        return None
    if not _context_is_fresh(context):
        context.request = None
        context.save(update_fields=["request", "updated_at"])
        return None
    return context.request


def touch_context(*, binding) -> None:
    OperatorConversationContext.objects.filter(binding=binding, request__isnull=False).update(
        updated_at=timezone.now()
    )


def invalidate_contexts(provider: str) -> int:
    """Require explicit re-entry and request selection after this worker restarts."""
    bindings = StaffMessengerBinding.objects.filter(provider=provider)
    bindings.update(operator_mode=False, updated_at=timezone.now())
    OwnerPhotoUploadContext.objects.filter(binding__in=bindings).delete()
    return OperatorConversationContext.objects.filter(binding__in=bindings).update(
        request=None, updated_at=timezone.now()
    )


def clear_context(*, binding):
    OperatorConversationContext.objects.filter(binding=binding).update(
        request=None, updated_at=timezone.now()
    )


@transaction.atomic
def revoke_binding(*, binding):
    """Disable one binding and destroy its active operator session."""
    binding = StaffMessengerBinding.objects.select_for_update().get(pk=binding.pk)
    binding.is_active = False
    binding.operator_mode = False
    binding.save(update_fields=["is_active", "operator_mode", "updated_at"])
    clear_context(binding=binding)
    clear_photo_context(binding=binding)
    clear_panel_delivery(binding=binding)
    StaffMessengerPairingToken.objects.filter(
        user=binding.user, revoked_at__isnull=True, used_at__isnull=True
    ).update(revoked_at=timezone.now())
    return binding


def card(request: CustomerRequest, *, binding=None) -> tuple[str, dict]:
    lines = [
        f"Заявка №{request.reference} - {request.customer_name}",
        f"Телефон: {request.customer_phone}",
        f"Создана: {timezone.localtime(request.created_at):%d.%m.%Y %H:%M}",
        f"Канал клиента: {request.get_preferred_messenger_display()}",
        "",
    ]
    total = 0
    for line in request.lines.all():
        price = "цена уточняется" if line.price_seen is None else f"{line.price_seen:.0f} ₽"
        lines.append(f"{line.part_name} · {line.quantity_requested:g} × {price}")
        if line.price_seen is not None:
            total += line.price_seen * line.quantity_requested
    lines.extend([f"Итого: {total:.0f} ₽" if total else "Итого: цена уточняется",
                  "", f"Статус: {request.get_status_display()}"])
    return "\n".join(lines), {"inline_keyboard": [
        [{"text": "Ответить", "callback_data": _callback(binding, "r", request.public_id.hex)}],
        [{"text": "К заявкам", "callback_data": _callback(binding, "m")}],
    ]}


def reply_prompt(request: CustomerRequest, *, binding=None) -> tuple[str, dict]:
    return (f"Активна заявка №{request.reference}. Напишите ответ клиенту.\n"
            "Получатель перепроверяется перед отправкой.",
            {"inline_keyboard": [[
                {"text": "Отмена", "callback_data": _callback(binding, "x")},
                {"text": "К заявкам", "callback_data": _callback(binding, "m")},
            ]]})


def _key(provider: str, external_id: str) -> str:
    return f"staff:{provider}:{_hash(external_id)}"


def submit_text(
    *, provider: str, provider_user_id: int, external_id: str, text: str, attachment=None,
    provider_chat_id: int | None = None,
):
    binding = binding_for(provider, provider_user_id, lock=True)
    if binding is None:
        return "Недоступно.", None
    if provider == StaffMessengerBinding.Provider.MAX and isinstance(provider_chat_id, int):
        if binding.delivery_chat_id != provider_chat_id:
            binding.delivery_chat_id = provider_chat_id
            binding.save(update_fields=["delivery_chat_id", "updated_at"])
    request = current_request(binding)
    if request is None:
        return "Сначала выберите заявку в рабочей панели.", menu(binding)[1]
    try:
        operator_replies.submit_reply(
            request_id=request.pk, user=binding.user, text=text,
            key=_key(provider, external_id), channel=request.preferred_messenger,
            attachment=attachment,
            operator_control_source=provider,
            operator_author_label=binding.customer_visible_label,
        )
    except operator_replies.OperatorReplyError as exc:
        return str(exc), menu(binding)[1]
    touch_context(binding=binding)
    return "Ответ поставлен в очередь доставки клиенту.", menu(binding)[1]


def handle_text(
    *, provider: str, provider_user_id: int, external_id: str, text: str, attachment=None,
    provider_chat_id: int | None = None,
):
    """Return a staff reply, or ``None`` so ordinary customer mode continues."""
    value = (text or "").strip()
    if is_pairing_code(value):
        binding, reply = consume_pairing(
            provider=provider, provider_user_id=provider_user_id, raw_token=value,
            provider_chat_id=provider_chat_id,
        )
        if binding is not None and enabled():
            return owner_panel(binding)
        return (reply, None) if reply else None
    if not enabled():
        return None
    binding = binding_for(provider, provider_user_id)
    lower = value.lower()
    if binding is None:
        return None
    if provider == StaffMessengerBinding.Provider.MAX and isinstance(provider_chat_id, int):
        if binding.delivery_chat_id != provider_chat_id:
            binding.delivery_chat_id = provider_chat_id
            binding.save(update_fields=["delivery_chat_id", "updated_at"])
    if lower in {"/work", "рабочее меню"}:
        clear_context(binding=binding)
        clear_photo_context(binding=binding)
        binding.operator_mode = True
        binding.save(update_fields=["operator_mode", "updated_at"])
        return menu(binding)
    if lower in {"/customer", "клиентский режим"}:
        clear_context(binding=binding)
        clear_photo_context(binding=binding)
        binding.operator_mode = False
        binding.save(update_fields=["operator_mode", "updated_at"])
        return "Клиентский режим включён.", None
    if not binding.operator_mode:
        return None
    if attachment is not None:
        receipt = OwnerPhotoUploadReceipt.objects.filter(
            binding=binding, external_id=str(external_id)
        ).first()
        if receipt is not None:
            return receipt.response_text, menu(binding)[1]
    photo_context = _photo_context(binding)
    if photo_context is not None:
        if lower in {"отмена", "/cancel"}:
            clear_photo_context(binding=binding)
            return "Загрузка фото отменена.", menu(binding)[1]
        if attachment is not None:
            return _consume_photo_upload(
                binding=binding, external_id=external_id, attachment=attachment
            )
        return (
            "Ожидается фотография выбранной детали. Нажмите «Отмена» или отправьте изображение.",
            {"inline_keyboard": [[{"text": "Отмена", "callback_data": _callback(binding, "x")}]]},
        )
    if lower in {"/menu", "меню", "рабочее меню"}:
        return menu(binding)
    if lower in {"/requests", "все заявки"}:
        return request_page(binding=binding)
    if lower in {"/new", "новые заявки"}:
        return request_page(new_only=True, binding=binding)
    if lower in {"/cancel", "отмена"}:
        clear_context(binding=binding)
        return (
            "Активная заявка закрыта для телефона. Клиенту ничего не отправлено.",
            menu(binding)[1],
        )
    return submit_text(provider=provider, provider_user_id=provider_user_id,
                       external_id=external_id, text=value, attachment=attachment,
                       provider_chat_id=provider_chat_id)


def handle_callback(*, provider: str, provider_user_id: int, payload: str):
    if not enabled() or not payload.startswith("op:"):
        return None
    binding = binding_for(provider, provider_user_id)
    if binding is None:
        return "Недоступно.", None
    parts = payload.split(":")
    if len(parts) not in {3, 4}:
        return "Рабочая сессия устарела. Откройте рабочую панель.", None
    kind, token = parts[1], parts[2]
    if token != _session_token(binding):
        return "Рабочая сессия устарела. Откройте рабочую панель.", None
    if kind not in {"m", "l", "n", "x", "c", "r", "p", "o", "q"}:
        return "Недоступно.", None
    if kind in {"p", "o", "q"} and provider != StaffMessengerBinding.Provider.TELEGRAM:
        return "Недоступно.", None
    if not binding.operator_mode:
        binding.operator_mode = True
        binding.save(update_fields=["operator_mode", "updated_at"])
    value = parts[3] if len(parts) == 4 else ""
    if kind == "m":
        return menu(binding)
    if kind in {"l", "n"}:
        page = int(value) if value.isdigit() and len(value) < 6 else 1
        return request_page(page, new_only=kind == "n", binding=binding)
    if kind == "x":
        had_photo_context = _photo_context(binding) is not None
        clear_photo_context(binding=binding)
        clear_context(binding=binding)
        return (
            (
                "Загрузка фото отменена."
                if had_photo_context
                else "Активная заявка закрыта для телефона. Клиенту ничего не отправлено."
            ),
            menu(binding)[1],
        )
    if kind == "p":
        page = int(value) if value.isdigit() and len(value) < 6 else 1
        return photo_operation_page(page, binding=binding)
    if kind == "o":
        if "-" not in value:
            return "Операция не найдена.", menu(binding)[1]
        operation_kind, operation_id = value.rsplit("-", 1)
        if operation_kind not in {"sale", "repair"} or not operation_id.isdigit():
            return "Операция не найдена.", menu(binding)[1]
        return photo_operation_card(
            binding=binding, kind=operation_kind, operation_id=int(operation_id)
        )
    if kind == "q":
        pieces = value.split("-")
        if len(pieces) != 3 or not pieces[1].isdigit() or not pieces[2].isdigit():
            return "Позиция не найдена.", menu(binding)[1]
        return _photo_selection(
            binding=binding,
            kind=pieces[0],
            operation_id=int(pieces[1]),
            part_id=int(pieces[2]),
        )
    if kind in {"c", "r"}:
        request = request_by_hex(value)
        if request is None:
            return "Заявка не найдена.", menu(binding)[1]
        selected, error = set_context(binding=binding, request_id=request.pk)
        if error:
            return error, menu(binding)[1]
        binding = binding_for(provider, provider_user_id)
        return card(selected, binding=binding) if kind == "c" else reply_prompt(
            selected, binding=binding
        )
    return "Недоступно.", None


def owner_panel(binding) -> tuple[str, dict]:
    """The reusable panel delivered by the explicit server/admin action."""
    return menu(binding)


def notification_for(request, binding, *, kind=OperatorNotification.Kind.NEW_REQUEST,
                     preview="", identity=""):
    if not enabled() or not binding.is_active:
        return None
    digest = _hash(f"{identity}:{preview}")[:16]
    return OperatorNotification.objects.get_or_create(
        binding=binding, request=request, kind=kind,
        dedupe_key=f"{kind}:{request.pk}:{binding.pk}:{digest}",
        defaults={"preview": preview[:700], "next_attempt_at": timezone.now()},
    )[0]


def clear_panel_delivery(*, binding) -> None:
    OperatorNotification.objects.filter(
        binding=binding, kind=OperatorNotification.Kind.OWNER_PANEL
    ).delete()


def queue_owner_panel(*, binding, refresh: bool = False):
    """Queue one explicit panel delivery without sending from the web process."""
    if (
        not enabled()
        or not binding.is_active
        or not binding.user.is_active
        or not binding.user.can_manage_sales
    ):
        return None, False
    row, created = OperatorNotification.objects.get_or_create(
        binding=binding,
        request=None,
        kind=OperatorNotification.Kind.OWNER_PANEL,
        dedupe_key=f"owner-panel:{binding.pk}",
        defaults={"next_attempt_at": timezone.now()},
    )
    if refresh and not created:
        row.status = OperatorNotification.Status.PENDING
        row.attempts = 0
        row.next_attempt_at = timezone.now()
        row.external_message_id = ""
        row.last_error = ""
        row.sent_at = None
        row.save(
            update_fields=[
                "status",
                "attempts",
                "next_attempt_at",
                "external_message_id",
                "last_error",
                "sent_at",
            ]
        )
    return row, created


def queue_new_request_notifications(*, since):
    if not enabled() or since is None:
        return 0
    bindings = list(StaffMessengerBinding.objects.filter(is_active=True, user__is_active=True))
    total = 0
    for request in CustomerRequest.objects.filter(
        status=CustomerRequest.Status.NEW, created_at__gte=since
    ).iterator():
        for binding in bindings:
            if binding.user.can_manage_sales:
                notification_for(request, binding)
                total += 1
    return total


def queue_operator_notifications(*, since):
    """Discover new requests and customer messages without public-role writes."""
    total = queue_new_request_notifications(since=since)
    if not enabled() or since is None:
        return total
    bindings = list(StaffMessengerBinding.objects.filter(is_active=True, user__is_active=True))
    sources = (
        (TelegramMessage, TelegramMessage.Direction.CUSTOMER),
        (MaxMessage, MaxMessage.Direction.CUSTOMER),
    )
    for model, direction in sources:
        for message in model.objects.select_related("conversation__request").filter(
            direction=direction, created_at__gte=since
        ).iterator():
            request = message.conversation.request
            preview = (message.text or "Вложение")[:700]
            for binding in bindings:
                if binding.user.can_manage_sales:
                    notification_for(
                        request, binding,
                        kind=OperatorNotification.Kind.CUSTOMER_MESSAGE,
                        preview=preview,
                        identity=f"{model.__name__}:{message.pk}",
                    )
                    total += 1
    return total


def ensure_runtime():
    with transaction.atomic():
        runtime, _ = OperatorConsoleRuntime.objects.select_for_update().get_or_create(
            pk=OperatorConsoleRuntime.SINGLETON_PK
        )
        if runtime.announce_requests_since is None:
            runtime.announce_requests_since = timezone.now()
            runtime.save(update_fields=["announce_requests_since", "updated_at"])
        return runtime


def notification_content(notification: OperatorNotification) -> tuple[str, dict]:
    if notification.kind == OperatorNotification.Kind.OWNER_PANEL:
        return owner_panel(notification.binding)
    request = notification.request
    binding = notification.binding
    name = (request.customer_name or "Клиент")[:80]
    if notification.kind == OperatorNotification.Kind.CUSTOMER_MESSAGE and notification.preview:
        text = f"Новое сообщение по заявке №{request.reference}\n{name}\n{notification.preview}"
    else:
        text = f"Новая заявка №{request.reference}\n{name}\n{request.lines.count()} поз."
    return text, {"inline_keyboard": [
        [{"text": "Открыть заявку", "callback_data": _callback(
            binding, "c", request.public_id.hex
        )}],
        [{"text": "Все заявки", "callback_data": _callback(binding, "l", "1")}],
    ]}


@dataclass(frozen=True)
class PreparedNotificationDelivery:
    """Immutable owner-notification routing copied while its rows are locked."""

    notification_id: int
    provider_user_id: int
    delivery_chat_id: int | None
    text: str
    buttons: dict


def _finish_locked_notification(
    notification, *, status: str, external_id: str = "", error: str = ""
):
    notification.status = status
    notification.last_error = str(error)[:255]
    if status == OperatorNotification.Status.SENT:
        notification.external_message_id = str(external_id or "")[:512]
        notification.sent_at = timezone.now()
    notification.save(update_fields=["status", "last_error", "external_message_id", "sent_at"])


def prepare_notification_delivery(*, notification_id: int, provider: str):
    """Lock and validate one claimed delivery without holding locks for provider I/O."""
    provider = _valid_provider(provider)
    if provider is None:
        return None
    with transaction.atomic():
        notification = (
            OperatorNotification.objects.select_for_update()
            .select_related("binding__user")
            .filter(
                pk=notification_id,
                binding__provider=provider,
                status=OperatorNotification.Status.SENDING,
            )
            .first()
        )
        if notification is None:
            return None
        binding = binding_for(provider, notification.binding.provider_user_id, lock=True)
        if binding is None or binding.pk != notification.binding_id:
            _finish_locked_notification(
                notification,
                status=OperatorNotification.Status.FAILED,
                error="Сотрудник отключён или привязка изменилась",
            )
            return None
        delivery_chat_id = binding.delivery_chat_id
        if provider == StaffMessengerBinding.Provider.MAX and not delivery_chat_id:
            _finish_locked_notification(
                notification,
                status=OperatorNotification.Status.FAILED,
                error="Неизвестен диалог сотрудника MAX",
            )
            return None
        notification.binding = binding
        text, buttons = notification_content(notification)
        return PreparedNotificationDelivery(
            notification_id=notification.pk,
            provider_user_id=binding.provider_user_id,
            delivery_chat_id=delivery_chat_id,
            text=text,
            buttons=buttons,
        )


def claim_notifications(provider: str, limit: int = 20):
    """Claim only this bot's rows; Telegram and MAX never deliver each other's work."""
    now = timezone.now()
    with transaction.atomic():
        ids = list(
            OperatorNotification.objects.select_for_update(
                skip_locked=True
            ).filter(
                binding__provider=provider,
                status=OperatorNotification.Status.PENDING,
                next_attempt_at__lte=now,
            ).order_by("pk").values_list("pk", flat=True)[:limit]
        )
        OperatorNotification.objects.filter(pk__in=ids).update(
            status=OperatorNotification.Status.SENDING, attempts=F("attempts") + 1
        )
    return list(
        OperatorNotification.objects.select_related("binding__user", "request")
        .filter(pk__in=ids)
        .order_by("pk")
    )


def recover_interrupted_notifications(provider: str) -> int:
    """Never leave a crashed worker's notification in a permanent spinner state."""
    note = "Отправка уведомления прервана остановкой бота; повторно не отправлялось."
    return OperatorNotification.objects.filter(
        binding__provider=provider,
        status=OperatorNotification.Status.SENDING,
    ).update(status=OperatorNotification.Status.UNCERTAIN, last_error=note)


def finish_notification(row, *, status: str, external_id: str = "", error: str = ""):
    with transaction.atomic():
        notification = OperatorNotification.objects.select_for_update().get(pk=row.pk)
        if notification.status != OperatorNotification.Status.SENDING:
            return
        _finish_locked_notification(
            notification, status=status, external_id=external_id, error=error
        )


def retry_notification(row, error):
    with transaction.atomic():
        notification = OperatorNotification.objects.select_for_update().get(pk=row.pk)
        if notification.status != OperatorNotification.Status.SENDING:
            return
        if notification.attempts >= 8:
            _finish_locked_notification(
                notification, status=OperatorNotification.Status.FAILED, error=error
            )
            return
        notification.status = OperatorNotification.Status.PENDING
        notification.next_attempt_at = timezone.now() + timedelta(
            seconds=min(900, 10 * 2 ** (notification.attempts - 1))
        )
        notification.last_error = str(error)[:255]
        notification.save(update_fields=["status", "next_attempt_at", "last_error"])


def recover_owner_panel_notifications(*, notification_ids) -> int:
    """Mark only proven pre-send owner-panel attempts uncertain, never resend them."""
    ids = sorted({int(value) for value in notification_ids if int(value) > 0})
    if not ids:
        raise ValueError("Не указаны уведомления панели владельца.")
    with transaction.atomic():
        rows = list(
            OperatorNotification.objects.select_for_update()
            .filter(pk__in=ids)
            .order_by("pk")
        )
        if len(rows) != len(ids):
            raise ValueError("Не все уведомления панели владельца найдены.")
        for row in rows:
            if (
                row.kind != OperatorNotification.Kind.OWNER_PANEL
                or row.status != OperatorNotification.Status.SENDING
                or row.request_id is not None
                or row.external_message_id
            ):
                raise ValueError("Уведомление не является безопасным pre-send recovery-кандидатом.")
        note = "Отправка панели прервана до подтверждения провайдера; повторно не отправлялась."
        OperatorNotification.objects.filter(pk__in=ids).update(
            status=OperatorNotification.Status.UNCERTAIN,
            last_error=note,
            next_attempt_at=None,
        )
    return len(ids)
