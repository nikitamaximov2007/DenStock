"""Opt-in mobile operator console for explicitly paired staff identities."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import F
from django.utils import timezone

from . import operator_replies, workspace
from .models import (
    CustomerRequest,
    MaxMessage,
    OperatorConsoleRuntime,
    OperatorConversationContext,
    OperatorNotification,
    StaffMessengerBinding,
    StaffMessengerPairingToken,
    TelegramMessage,
)

LIST_PAGE_SIZE = 8
PAIRING_TTL = timedelta(minutes=10)


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


@transaction.atomic
def issue_pairing_token(*, user, provider: str, label: str, created_by) -> str:
    if provider not in StaffMessengerPairingToken.Provider.values:
        raise ValueError("Неизвестный мессенджер.")
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise ValueError("Укажите подпись сотрудника длиной до 80 символов.")
    raw = secrets.token_urlsafe(32)
    StaffMessengerPairingToken.objects.create(
        token_hash=_hash(raw),
        user=user,
        provider=provider,
        customer_visible_label=label,
        expires_at=timezone.now() + PAIRING_TTL,
        created_by=created_by,
    )
    return f"pair_{raw}"


@transaction.atomic
def consume_pairing(
    *, provider: str, provider_user_id: int, raw_token: str, provider_chat_id: int | None = None
):
    if not enabled():
        return None, "Мобильная консоль отключена."
    if provider not in StaffMessengerPairingToken.Provider.values:
        return None, "Код привязки недействителен."
    token = (raw_token or "").strip()
    token = token[5:] if token.startswith("pair_") else token
    row = (
        StaffMessengerPairingToken.objects.select_for_update()
        .select_related("user")
        .filter(
            provider=provider,
            token_hash=_hash(token),
            used_at__isnull=True,
            revoked_at__isnull=True,
            expires_at__gt=timezone.now(),
        )
        .first()
    )
    if row is None or not row.user.is_active or not row.user.can_manage_sales:
        return None, "Код привязки недействителен или истёк."
    existing = StaffMessengerBinding.objects.filter(
        provider=provider, provider_user_id=provider_user_id
    ).first()
    if existing and existing.is_active:
        return None, "Этот аккаунт мессенджера уже привязан."
    if existing and existing.user_id != row.user_id:
        return None, "Этот аккаунт уже принадлежит другому сотруднику."
    if existing is None and StaffMessengerBinding.objects.filter(
        user=row.user, provider=provider
    ).exists():
        return None, "У сотрудника уже есть привязка этого мессенджера."
    if existing is not None:
        binding = existing
        binding.is_active = True
        binding.operator_mode = False
        clear_context(binding=binding)
        binding.customer_visible_label = row.customer_visible_label
        binding.created_by = row.created_by
        if provider == StaffMessengerBinding.Provider.MAX and isinstance(provider_chat_id, int):
            binding.delivery_chat_id = provider_chat_id
        binding.save()
    else:
        binding = StaffMessengerBinding.objects.create(
            user=row.user,
            provider=provider,
            provider_user_id=provider_user_id,
            delivery_chat_id=(
                provider_chat_id if provider == StaffMessengerBinding.Provider.MAX else None
            ),
            customer_visible_label=row.customer_visible_label,
            created_by=row.created_by,
        )
    row.used_at = timezone.now()
    row.save(update_fields=["used_at"])
    return binding, f"Привязка завершена. Подпись для клиента: {binding.customer_visible_label}."


def _context_is_fresh(context) -> bool:
    ttl = max(1, int(getattr(settings, "CUSTOMER_OPERATOR_CONTEXT_TTL_MINUTES", 30)))
    return context.updated_at >= timezone.now() - timedelta(minutes=ttl)


def _session_token(binding) -> str:
    """Opaque token for the current binding session."""
    context, _ = OperatorConversationContext.objects.get_or_create(binding=binding)
    return _hash(f"operator-session:{binding.pk}:{context.updated_at.isoformat()}")[:16]


def _callback(binding, kind: str, value: str = "") -> str:
    token = _session_token(binding)
    suffix = f":{value}" if value else ""
    return f"op:{kind}:{token}{suffix}"


def menu(binding=None) -> tuple[str, dict]:
    heading = "Рабочее меню PRO-STOR"
    if binding is not None:
        context = OperatorConversationContext.objects.select_related("request").filter(
            binding=binding
        ).first()
        if context and context.request_id and _context_is_fresh(context):
            request = context.request
            heading += (
                f"\nСейчас открыт диалог: №{request.reference} — "
                f"{(request.customer_name or 'Клиент')[:80]}"
            )
    return heading, {"inline_keyboard": [
        [{"text": "Все заявки", "callback_data": _callback(binding, "l", "1")}],
        [{"text": "Новые заявки", "callback_data": _callback(binding, "n", "1")}],
    ]}


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
        rows.append([{"text": f"№{request.reference} — {(request.customer_name or 'Клиент')[:80]}",
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
    return binding


def card(request: CustomerRequest, *, binding=None) -> tuple[str, dict]:
    lines = [
        f"Заявка №{request.reference} — {request.customer_name}",
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
        return "Сначала выберите заявку в разделе «Все заявки».", menu(binding)[1]
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
    if not enabled():
        return None
    binding = binding_for(provider, provider_user_id)
    value = (text or "").strip()
    lower = value.lower()
    if lower.startswith("/pair ") or lower.startswith("pair_"):
        raw = value.split(maxsplit=1)[1] if lower.startswith("/pair ") else value
        _binding, reply = consume_pairing(
            provider=provider, provider_user_id=provider_user_id, raw_token=raw,
            provider_chat_id=provider_chat_id,
        )
        return reply, menu(_binding)[1] if _binding else None
    if binding is None:
        return None
    if provider == StaffMessengerBinding.Provider.MAX and isinstance(provider_chat_id, int):
        if binding.delivery_chat_id != provider_chat_id:
            binding.delivery_chat_id = provider_chat_id
            binding.save(update_fields=["delivery_chat_id", "updated_at"])
    if lower in {"/work", "рабочее меню"}:
        clear_context(binding=binding)
        binding.operator_mode = True
        binding.save(update_fields=["operator_mode", "updated_at"])
        return menu(binding)
    if lower in {"/customer", "клиентский режим"}:
        clear_context(binding=binding)
        binding.operator_mode = False
        binding.save(update_fields=["operator_mode", "updated_at"])
        return "Клиентский режим включён.", None
    if not binding.operator_mode:
        return None
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
    if not binding.operator_mode:
        return "Рабочий режим не активен. Откройте /work.", None
    parts = payload.split(":")
    if len(parts) not in {3, 4}:
        return "Рабочая сессия устарела. Откройте /work.", None
    kind, token = parts[1], parts[2]
    if token != _session_token(binding):
        return "Рабочая сессия устарела. Откройте /work.", None
    value = parts[3] if len(parts) == 4 else ""
    if kind == "m":
        return menu(binding)
    if kind in {"l", "n"}:
        page = int(value) if value.isdigit() and len(value) < 6 else 1
        return request_page(page, new_only=kind == "n", binding=binding)
    if kind == "x":
        clear_context(binding=binding)
        return (
            "Активная заявка закрыта для телефона. Клиенту ничего не отправлено.",
            menu(binding)[1],
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
    row.status = status
    row.last_error = str(error)[:255]
    if status == OperatorNotification.Status.SENT:
        row.external_message_id = str(external_id or "")[:512]
        row.sent_at = timezone.now()
    row.save(update_fields=["status", "last_error", "external_message_id", "sent_at"])


def retry_notification(row, error):
    if row.attempts >= 8:
        finish_notification(row, status=OperatorNotification.Status.FAILED, error=error)
        return
    row.status = OperatorNotification.Status.PENDING
    row.next_attempt_at = timezone.now() + timedelta(seconds=min(900, 10 * 2 ** (row.attempts - 1)))
    row.last_error = str(error)[:255]
    row.save(update_fields=["status", "next_attempt_at", "last_error"])
