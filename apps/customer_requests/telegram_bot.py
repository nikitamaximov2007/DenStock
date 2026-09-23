"""Long-polling Telegram worker: updates in, durable outbox out.

One process consumes one bot token. Guarantees:

* an update and its offset are committed together, so a restart neither loses
  nor re-applies it (stored messages are also unique per update id);
* a customer message is sent at most once: the row is marked ``sending`` in a
  committed transaction before the network call, and a row found ``sending``
  after a restart becomes ``uncertain`` instead of being resent;
* definite refusals and network failures before sending are retried with a
  bounded backoff; permanent refusals become ``failed``; both stay visible;
* only one worker runs: a PostgreSQL advisory lock plus a lease row, and
  Telegram's own 409 answer, all stop a second consumer.
"""
from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import DatabaseError, connection, transaction
from django.db.models import F, Q
from django.utils import timezone

from apps.customer_accounts import messenger_hooks as account_hooks
from apps.operations.models import TelegramBotRuntime
from apps.operations.write_guard import BusinessWriteBlocked

from . import customer_ui, messaging, operator_bot, operator_console, operator_replies
from . import telegram_service as service
from .attachments import (
    AttachmentError,
    AttachmentStorageError,
    cleanup_attachment,
    read_attachment,
    validate_attachment,
)
from .messengers import MessengerLinkError, consume_telegram_start
from .models import (
    MaxDeliveryStatus,
    MaxOperatorDelivery,
    OperatorNotification,
    TelegramDelivery,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOutboxEvent,
)
from .telegram_api import TelegramApiError, TelegramError, TelegramNetworkError

logger = logging.getLogger("apps.customer_requests.telegram_bot")

ADVISORY_LOCK_ID = 0x4453544742_4F54  # "DSTGBOT"
LEASE_SECONDS = 90
MAX_ATTEMPTS = 8
EVENT_MAX_AGE = timedelta(days=7)
NO_OPERATOR_RETRY = timedelta(minutes=5)
BATCH = 20
# Startup while Telegram is unreachable: wait in-process instead of exiting, so
# the container restart policy never turns an outage into a rapid loop.
STARTUP_BACKOFF_BASE_SECONDS = 5
STARTUP_BACKOFF_MAX_SECONDS = 300
LEASE_RENEW_SLICE_SECONDS = 30


class SingleInstanceError(RuntimeError):
    """Another consumer owns this bot, or Telegram delivers updates elsewhere."""


@dataclass(frozen=True, slots=True)
class Outgoing:
    """A reply that may be lost harmlessly (menus, cards, hints).

    ``edit_message_id`` re-renders a message the bot already sent instead of
    sending a new one: the selector's ✓ marker moves in place. The customer's
    choice is already stored, so losing this changes nothing they rely on.
    """

    chat_id: int | None = None
    text: str = ""
    reply_markup: dict | None = None
    callback_query_id: str = ""
    callback_text: str = ""
    edit_message_id: int | None = None


def _rerendered_selector(chat_id: int, message_id) -> Outgoing:
    """The selector as it looks after the press, for the message that holds it."""
    if not _is_int(message_id):
        return Outgoing()
    text, markup = service.selector_text_and_markup(chat_id)
    return Outgoing(
        chat_id=chat_id, text=text, reply_markup=markup, edit_message_id=message_id
    )


def backoff_seconds(attempts: int) -> int:
    return min(10 * 2 ** max(attempts - 1, 0), 900)


def startup_backoff_seconds(attempt: int) -> int:
    return min(
        STARTUP_BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), STARTUP_BACKOFF_MAX_SECONDS
    )


def back_to_list() -> dict:
    return operator_bot.back_to_list()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _telegram_attachment_descriptor(message: dict) -> tuple[str, str] | None:
    document = message.get("document")
    if isinstance(document, dict) and document.get("file_id"):
        filename = document.get("file_name")
        if not filename:
            mime = str(document.get("mime_type") or "").lower()
            extension = {
                "image/png": "png",
                "image/jpeg": "jpg",
                "image/webp": "webp",
            }.get(mime, "pdf")
            filename = f"document.{extension}"
        return str(document["file_id"]), str(filename)
    photos = message.get("photo")
    if isinstance(photos, list):
        candidates = [item for item in photos if isinstance(item, dict) and item.get("file_id")]
        if candidates:
            photo = max(candidates, key=lambda item: int(item.get("file_size") or 0))
            return str(photo["file_id"]), "photo.jpg"
    return None


# --- Update handling (runs inside the caller's transaction) -------------------------------


def handle_update(update, *, attachment_loader=None) -> list[Outgoing]:
    if not isinstance(update, dict):
        return []
    if isinstance(update.get("callback_query"), dict):
        return _handle_callback(update["callback_query"])
    message = update.get("message")
    if not isinstance(message, dict):
        return []
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat_id, user_id, update_id = chat.get("id"), sender.get("id"), update.get("update_id")
    # Private chats only: a group must never become a customer or an operator.
    if chat.get("type") != "private" or not all(map(_is_int, (chat_id, user_id, update_id))):
        return []

    def reply(text, markup=None):
        return [Outgoing(chat_id=chat_id, text=text, reply_markup=markup)] if text else []

    text = message.get("text")
    attachment = None
    has_attachment = isinstance(message.get("document"), dict) or isinstance(
        message.get("photo"), list
    )
    if has_attachment and operator_console.enabled() and operator_console.binding_for(
        "telegram", user_id
    ):
        if attachment_loader is None:
            return reply(service.MEDIA_NOT_SUPPORTED_TEXT)
        try:
            attachment = attachment_loader(message)
        except (AttachmentError, TelegramError):
            return reply("Не удалось прочитать вложение. Повторите отправку позже.")
        text = message.get("caption", "")
    if not isinstance(text, str):
        return reply(service.MEDIA_NOT_SUPPORTED_TEXT)
    text = text.strip()
    operator_reply = operator_console.handle_text(
        provider="telegram",
        provider_user_id=user_id,
        external_id=str(update_id),
        text=text,
        attachment=attachment,
        provider_chat_id=chat_id,
    )
    if operator_reply is not None:
        return reply(*operator_reply)
    command, argument = "", ""
    if text.startswith("/"):
        head, _, argument = text.partition(" ")
        command = head.split("@", 1)[0].lower()
        argument = argument.strip()

    if command == "/start" and argument:
        # A link from the website's «Подключить Telegram» is an account event.
        account_reply = account_hooks.telegram_start(
            argument=argument, user_id=user_id, chat_id=chat_id, user=sender
        )
        if account_reply is not None:
            return reply(account_reply)
        try:
            consume_telegram_start(
                token=argument,
                chat_id=chat_id,
                user_id=user_id,
                username=str(sender.get("username") or ""),
            )
        except MessengerLinkError:
            return reply(service.LINK_INVALID_TEXT)
        return []  # the confirmation is a stored message delivered by the outbox

    if service.authorized_operator(user_id) is not None:
        try:
            if command in {"/start", "/menu"}:
                return reply(*service.operator_menu())
            if command == "/requests":
                return reply(*service.operator_request_page(1))
            if command == "/cancel":
                return reply(service.cancel_reply(telegram_user_id=user_id), back_to_list())
            if command:
                return reply(service.OPERATOR_HELP_TEXT, back_to_list())
            answer = service.submit_operator_reply(
                telegram_user_id=user_id, update_id=update_id, text=text
            )
            return reply(*answer) if isinstance(answer, tuple) else reply(answer)
        except service.TelegramAccessDenied:
            return reply(service.NOT_AVAILABLE_TEXT)

    def customer(result):
        return reply(result.reply, result.keyboard)

    if text.strip().lower() in service.MY_REQUESTS_TEXTS:
        # The persistent keyboard sends plain text, not a command.
        return customer(service.customer_conversations_prompt(chat_id))
    if text.strip().lower() in service.MY_PURCHASES_TEXTS and service.customer_cabinet_enabled():
        return customer(service.purchase_selector_result(chat_id, provider_user_id=user_id))
    if command in {"/start", "/help"}:
        return customer(service.customer_greeting(chat_id))
    if command:
        return customer(service.customer_greeting(chat_id))
    return customer(
        service.record_customer_message(chat_id=chat_id, update_id=update_id, text=text)
    )


def _handle_callback(callback) -> list[Outgoing]:
    callback_id = callback.get("id")
    sender = callback.get("from") if isinstance(callback.get("from"), dict) else {}
    user_id = sender.get("id")
    data = callback.get("data")
    if not isinstance(callback_id, str) or not _is_int(user_id) or not isinstance(data, str):
        return []
    denied = [Outgoing(callback_query_id=callback_id, callback_text=service.NOT_AVAILABLE_TEXT)]
    answered = Outgoing(callback_query_id=callback_id)
    kind, _, value = data.partition(":")

    if data.startswith("op:"):
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        callback_chat_id = (message.get("chat") or {}).get("id", user_id)
        result = operator_console.handle_callback(
            provider="telegram", provider_user_id=user_id, payload=data
        )
        if result is None:
            return denied
        text, markup = result
        return [answered, Outgoing(chat_id=callback_chat_id, text=text, reply_markup=markup)]

    # An active staff identity never falls through to customer callbacks.
    if operator_console.enabled() and operator_console.binding_for("telegram", user_id):
        return denied

    if kind == "s":
        # Customer choosing among their own requests. In a private chat the
        # chat id equals the user id; the service re-checks ownership.
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        message_id = message.get("message_id")
        conversation = service.select_customer_conversation(
            chat_id=user_id, conversation_hex=value
        )
        if conversation is None:
            closed = service.closed_selection(chat_id=user_id, conversation_hex=value)
            if closed is None:
                return denied
            # The customer's own request has closed since this button was sent.
            return [
                answered,
                _rerendered_selector(user_id, message_id),
                Outgoing(chat_id=user_id, text=closed.reply, reply_markup=closed.keyboard),
            ]
        return [
            answered,
            _rerendered_selector(user_id, message_id),
            Outgoing(
                chat_id=user_id,
                text=customer_ui.selected_text(conversation.request.reference),
            ),
        ]

    if data == service.customer_ui.MY_PURCHASES_PAYLOAD and service.customer_cabinet_enabled():
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        callback_chat_id = (message.get("chat") or {}).get("id", user_id)
        result = service.purchase_selector_result(callback_chat_id, provider_user_id=user_id)
        return [
            answered,
            Outgoing(chat_id=callback_chat_id, text=result.reply, reply_markup=result.keyboard),
        ]

    if kind in {"p", "rc"} or (
        kind == "r" and value.isdigit() and service.authorized_operator(user_id) is None
    ):
        message = callback.get("message") if isinstance(callback.get("message"), dict) else {}
        message_id = message.get("message_id")
        callback_chat_id = (message.get("chat") or {}).get("id", user_id)
        if kind == "p":
            result = service.purchase_detail_result(
                chat_id=callback_chat_id, provider_user_id=user_id, sale_id=value
            )
        elif kind == "r":
            result = service.reorder_preview_result(
                chat_id=callback_chat_id, provider_user_id=user_id, sale_id=value
            )
        else:
            result = service.confirm_reorder_result(
                chat_id=callback_chat_id, provider_user_id=user_id,
                sale_id=value, callback_key=callback_id
            )
        return [
            answered,
            Outgoing(chat_id=callback_chat_id, text=result.reply, reply_markup=result.keyboard),
        ]

    # Every operator button re-authorizes; a hidden button is not authorization.
    if service.authorized_operator(user_id) is None:
        return denied
    try:
        if kind == "m":
            return [answered, Outgoing(user_id, *service.operator_menu())]
        if kind == "l":
            page = int(value) if value.isdigit() and len(value) < 6 else 1
            return [answered, Outgoing(user_id, *service.operator_request_page(page))]
        if kind == "c":
            request = operator_bot.request_by_hex(value)
            if request is None:
                return denied
            return [
                answered,
                Outgoing(
                    user_id,
                    service.request_card_text(request),
                    service.operator_buttons(request),
                ),
            ]
        if kind == "r":
            text, markup = service.begin_reply(telegram_user_id=user_id, conversation_hex=value)
            return [answered, Outgoing(user_id, text, markup)]
        if kind == "x":
            return [
                answered,
                Outgoing(user_id, service.cancel_reply(telegram_user_id=user_id), back_to_list()),
            ]
    except service.TelegramAccessDenied:
        return denied
    return denied


# --- Worker ------------------------------------------------------------------------------


class TelegramBotWorker:
    def __init__(self, api, *, stop: threading.Event | None = None, worker_id: str = "",
                 poll_timeout: int | None = None, heartbeat_file: str | None = None):
        self.api = api
        self.stop = stop or threading.Event()
        self.worker_id = worker_id or uuid.uuid4().hex
        self.poll_timeout = (
            settings.TELEGRAM_POLL_TIMEOUT_SECONDS if poll_timeout is None else poll_timeout
        )
        self.heartbeat_file = (
            settings.TELEGRAM_BOT_HEARTBEAT_FILE if heartbeat_file is None else heartbeat_file
        )
        self._holds_lock = False
        self._needs_recovery = False

    # Single instance -------------------------------------------------------------------

    def acquire(self) -> None:
        # The advisory lock first: a refused second instance must never touch
        # the lease row, or its exit would clear the running worker's lease.
        self._lock_database()
        now = timezone.now()
        with transaction.atomic():
            runtime, _ = TelegramBotRuntime.objects.select_for_update().get_or_create(
                pk=TelegramBotRuntime.SINGLETON_PK
            )
            if (
                runtime.worker_id
                and runtime.worker_id != self.worker_id
                and runtime.lease_expires_at
                and runtime.lease_expires_at > now
            ):
                raise SingleInstanceError(
                    "Другой экземпляр Telegram-бота уже работает (аренда не истекла)."
                )
            runtime.worker_id = self.worker_id
            runtime.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            runtime.started_at = runtime.heartbeat_at = now
            runtime.save()

    def _lock_database(self) -> None:
        if connection.vendor != "postgresql":
            return
        with connection.cursor() as cursor:
            if self._holds_lock:
                cursor.execute(
                    "SELECT 1 FROM pg_locks WHERE locktype = 'advisory' AND granted "
                    "AND pid = pg_backend_pid() AND objid = %s",
                    [ADVISORY_LOCK_ID & 0xFFFFFFFF],
                )
                if cursor.fetchone():
                    return
            cursor.execute("SELECT pg_try_advisory_lock(%s)", [ADVISORY_LOCK_ID])
            if not cursor.fetchone()[0]:
                raise SingleInstanceError("Другой экземпляр Telegram-бота держит блокировку.")
        self._holds_lock = True

    def renew(self) -> None:
        """Keep the single-consumer lease. Says nothing about Telegram itself."""
        now = timezone.now()
        renewed = TelegramBotRuntime.objects.filter(
            pk=TelegramBotRuntime.SINGLETON_PK, worker_id=self.worker_id
        ).update(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
        if not renewed:
            raise SingleInstanceError("Аренду Telegram-бота перехватил другой экземпляр.")
        self._lock_database()

    def _touch_heartbeat(self) -> None:
        """Container health: only after Telegram actually answered."""
        if self.heartbeat_file:
            try:
                Path(self.heartbeat_file).touch()
            except OSError:
                logger.warning("heartbeat file is not writable")

    def release(self) -> None:
        TelegramBotRuntime.objects.filter(
            pk=TelegramBotRuntime.SINGLETON_PK, worker_id=self.worker_id
        ).update(worker_id="", lease_expires_at=None)
        if self._holds_lock and connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_ID])
            self._holds_lock = False

    def record_error(self, text: str) -> None:
        TelegramBotRuntime.objects.filter(pk=TelegramBotRuntime.SINGLETON_PK).update(
            last_error=str(text)[:255], last_error_at=timezone.now()
        )

    # Recovery ----------------------------------------------------------------------------

    def recover_interrupted_sends(self) -> int:
        note = "Отправка прервана остановкой бота; повторно не отправлялось."
        messages = TelegramMessage.objects.filter(
            delivery_status=TelegramDeliveryStatus.SENDING
        ).update(delivery_status=TelegramDeliveryStatus.UNCERTAIN, last_error=note)
        deliveries = TelegramDelivery.objects.filter(
            status=TelegramDeliveryStatus.SENDING
        ).update(status=TelegramDeliveryStatus.UNCERTAIN, last_error=note)
        max_deliveries = MaxOperatorDelivery.objects.filter(
            status=MaxDeliveryStatus.SENDING
        ).update(status=MaxDeliveryStatus.UNCERTAIN, last_error=note)
        return messages + deliveries + max_deliveries

    # Updates -----------------------------------------------------------------------------

    def poll_once(self, timeout: int) -> int:
        runtime = TelegramBotRuntime.objects.get(pk=TelegramBotRuntime.SINGLETON_PK)
        updates = self.api.get_updates(offset=runtime.last_update_id + 1, timeout=timeout)
        processed = 0
        for update in updates:
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if not _is_int(update_id) or update_id <= runtime.last_update_id:
                continue
            try:
                with transaction.atomic():
                    outgoing = handle_update(
                        update, attachment_loader=self._load_operator_attachment
                    )
                    self._advance(update_id)
            except BusinessWriteBlocked:
                raise
            except Exception as exc:  # noqa: BLE001 - one poisoned update must not stop the bot
                logger.error("update %s failed: %s", update_id, type(exc).__name__)
                self.record_error(f"Обновление {update_id}: {type(exc).__name__}")
                with transaction.atomic():
                    self._advance(update_id)
                outgoing = []
            runtime.last_update_id = update_id
            processed += 1
            self._send_ephemeral(outgoing)
        return processed

    def _advance(self, update_id: int) -> None:
        TelegramBotRuntime.objects.filter(pk=TelegramBotRuntime.SINGLETON_PK).update(
            last_update_id=update_id, last_update_at=timezone.now()
        )

    def _load_operator_attachment(self, message: dict):
        descriptor = _telegram_attachment_descriptor(message)
        if descriptor is None:
            raise AttachmentError("Вложение не распознано.")
        file_id, filename = descriptor
        metadata = self.api.get_file(file_id)
        content = self.api.download_file(metadata["file_path"])
        return validate_attachment(ContentFile(content, name=filename))

    def _send_ephemeral(self, outgoing: list[Outgoing]) -> None:
        for item in outgoing:
            try:
                if item.callback_query_id:
                    self.api.answer_callback_query(
                        callback_query_id=item.callback_query_id, text=item.callback_text
                    )
                elif item.edit_message_id is not None and item.chat_id is not None and item.text:
                    self.api.edit_message_text(
                        chat_id=item.chat_id,
                        message_id=item.edit_message_id,
                        text=item.text,
                        reply_markup=item.reply_markup,
                    )
                elif item.chat_id is not None and item.text:
                    self.api.send_message(
                        chat_id=item.chat_id, text=item.text, reply_markup=item.reply_markup
                    )
            except TelegramError as exc:
                logger.warning("reply not sent: %s", exc)

    # Outbox ------------------------------------------------------------------------------

    def _locked(self, queryset):
        return queryset.select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked
        )

    def dispatch_events(self, limit: int = 50) -> int:
        now = timezone.now()
        with transaction.atomic():
            events = list(
                self._locked(
                    TelegramOutboxEvent.objects.filter(
                        status=TelegramOutboxEvent.Status.PENDING, next_attempt_at__lte=now
                    )
                ).order_by("pk")[:limit]
            )
            for event in events:
                operators = service.active_operators(exclude_id=event.exclude_operator_id)
                outcome = messaging.operator_event_outcome(
                    has_recipients=bool(operators),
                    excludes_author=event.exclude_operator_id is not None,
                    anyone_eligible=bool(operators)
                    or (
                        event.exclude_operator_id is not None
                        and bool(service.active_operators())
                    ),
                    expired=now - event.created_at > EVENT_MAX_AGE,
                )
                if outcome != messaging.EVENT_DELIVER:
                    # The shared rule (``messaging.operator_event_outcome``): an
                    # operator's own reply with nobody else to tell is complete
                    # with no delivery of its own; an event nobody can receive
                    # *yet* keeps waiting for an operator to appear.
                    if outcome == messaging.EVENT_COMPLETE:
                        event.status = TelegramOutboxEvent.Status.DISPATCHED
                        event.dispatched_at = now
                        event.save(update_fields=["status", "dispatched_at"])
                        continue
                    if outcome == messaging.EVENT_EXPIRE:
                        event.status = TelegramOutboxEvent.Status.EXPIRED
                    event.attempts += 1
                    event.next_attempt_at = now + NO_OPERATOR_RETRY
                    event.save(update_fields=["status", "attempts", "next_attempt_at"])
                    continue
                TelegramDelivery.objects.bulk_create(
                    [
                        TelegramDelivery(event=event, operator=operator, next_attempt_at=now)
                        for operator in operators
                    ],
                    ignore_conflicts=True,
                )
                event.status = TelegramOutboxEvent.Status.DISPATCHED
                event.dispatched_at = now
                event.save(update_fields=["status", "dispatched_at"])
        return len(events)

    def _claim(self, model, status_field: str, extra: Q, limit: int) -> list:
        now = timezone.now()
        with transaction.atomic():
            ids = list(
                self._locked(
                    model.objects.filter(
                        extra,
                        **{status_field: TelegramDeliveryStatus.PENDING},
                        next_attempt_at__lte=now,
                    )
                )
                .order_by("pk")
                .values_list("pk", flat=True)[:limit]
            )
            model.objects.filter(pk__in=ids).update(
                **{status_field: TelegramDeliveryStatus.SENDING}, attempts=F("attempts") + 1
            )
        return ids

    def _finish(self, row, status_field: str, status: str, *, message_id=None, error="") -> None:
        values = {status_field: status, "last_error": str(error)[:255]}
        if status == TelegramDeliveryStatus.SENT:
            values.update(sent_at=timezone.now(), telegram_message_id=message_id)
        # Use save() so the post_save signal emits a WorkspaceEvent and an
        # already-open operator workspace reconciles the delivery status.
        for field, value in values.items():
            setattr(row, field, value)
        update_fields = [*values, "updated_at"] if hasattr(row, "updated_at") else list(values)
        row.save(update_fields=update_fields)
        if status in (TelegramDeliveryStatus.SENT, TelegramDeliveryStatus.FAILED):
            cleanup_attachment(row)

    def _fail(self, row, status_field: str, exc: TelegramError) -> None:
        if isinstance(exc, TelegramNetworkError) and exc.ambiguous:
            return self._finish(row, status_field, TelegramDeliveryStatus.UNCERTAIN, error=exc)
        retryable = isinstance(exc, TelegramNetworkError) or (
            isinstance(exc, TelegramApiError) and exc.retryable
        )
        if not retryable or row.attempts >= MAX_ATTEMPTS:
            return self._finish(row, status_field, TelegramDeliveryStatus.FAILED, error=exc)
        delay = getattr(exc, "retry_after", None) or backoff_seconds(row.attempts)
        type(row).objects.filter(pk=row.pk).update(
            **{status_field: TelegramDeliveryStatus.PENDING},
            next_attempt_at=timezone.now() + timedelta(seconds=delay),
            last_error=str(exc)[:255],
        )

    def send_customer_messages(self, limit: int = BATCH) -> int:
        outbound = Q(
            direction__in=[TelegramMessage.Direction.OPERATOR, TelegramMessage.Direction.SYSTEM]
        )
        ids = self._claim(TelegramMessage, "delivery_status", outbound, limit)
        rows = TelegramMessage.objects.select_related("conversation__request").filter(pk__in=ids)
        for row in rows.order_by("pk"):
            conversation = row.conversation
            if not conversation.is_linked or not service.customer_contact_allowed(
                conversation.request
            ):
                self._finish(
                    row, "delivery_status", TelegramDeliveryStatus.FAILED,
                    error="Клиент недоступен для сообщений",
                )
                continue
            try:
                if row.attachment:
                    content = read_attachment(row.attachment)
                    result = self.api.send_file(
                        chat_id=conversation.customer_chat_id,
                        content=content,
                        filename=row.attachment_name or row.attachment.name.rsplit("/", 1)[-1],
                        content_type=row.attachment_content_type,
                        caption=row.text,
                        reply_markup=service.customer_keyboard(),
                    )
                else:
                    result = self.api.send_message(
                        chat_id=conversation.customer_chat_id,
                        text=row.text,
                        reply_markup=service.customer_keyboard(),
                    )
            except AttachmentStorageError as exc:
                self._finish(row, "delivery_status", TelegramDeliveryStatus.FAILED, error=exc)
                continue
            except TelegramError as exc:
                self._fail(row, "delivery_status", exc)
                continue
            self._finish(
                row, "delivery_status", TelegramDeliveryStatus.SENT,
                message_id=(result or {}).get("message_id"),
            )
            operator_replies.confirm_responder_transition(row)
        return len(ids)

    def send_operator_deliveries(self, limit: int = BATCH) -> int:
        ids = self._claim(TelegramDelivery, "status", Q(), limit)
        rows = TelegramDelivery.objects.select_related(
            "operator__user", "event__request", "event__message__operator_user"
        ).filter(pk__in=ids)
        for row in rows.order_by("pk"):
            if not service.operator_is_authorized(row.operator):
                self._finish(row, "status", TelegramDeliveryStatus.FAILED,
                             error="Сотрудник отключён")
                continue
            text, markup = service.delivery_content(row.event)
            try:
                result = self.api.send_message(
                    chat_id=row.operator.telegram_user_id, text=text, reply_markup=markup
                )
            except TelegramError as exc:
                self._fail(row, "status", exc)
                continue
            self._finish(
                row, "status", TelegramDeliveryStatus.SENT,
                message_id=(result or {}).get("message_id"),
            )
        return len(ids)

    def send_max_operator_deliveries(self, limit: int = BATCH) -> int:
        """Employees hear about MAX requests through this same operators' bot.

        The rows belong to MAX (``MaxOperatorDelivery``) and name a DenisStock
        user; this bot is only the way to reach that employee. Status values
        are shared with Telegram's, so the claim and retry rules are identical.
        """
        from . import max_service

        ids = self._claim(MaxOperatorDelivery, "status", Q(), limit)
        rows = MaxOperatorDelivery.objects.select_related(
            "recipient__telegram_operator__user", "event__request", "event__message__operator_user"
        ).filter(pk__in=ids)
        for row in rows.order_by("pk"):
            operator = max_service.notification_operator(row.recipient)
            if operator is None:
                self._finish(row, "status", TelegramDeliveryStatus.FAILED,
                             error="Сотрудник отключён")
                continue
            text, markup = max_service.delivery_content(row.event)
            try:
                result = self.api.send_message(
                    chat_id=operator.telegram_user_id, text=text, reply_markup=markup
                )
            except TelegramError as exc:
                self._fail(row, "status", exc)
                continue
            self._finish(
                row, "status", TelegramDeliveryStatus.SENT,
                message_id=(result or {}).get("message_id"),
            )
        return len(ids)

    def send_operator_console_notifications(self, limit: int = BATCH) -> int:
        if not operator_console.enabled():
            return 0
        rows = operator_console.claim_notifications("telegram", limit)
        for row in rows:
            try:
                delivery = operator_console.prepare_notification_delivery(
                    notification_id=row.pk, provider="telegram"
                )
            except Exception as exc:
                operator_console.retry_notification(row, exc)
                continue
            if delivery is None:
                continue
            try:
                result = self.api.send_message(
                    chat_id=delivery.provider_user_id,
                    text=delivery.text,
                    reply_markup=delivery.buttons,
                )
            except TelegramError as exc:
                if isinstance(exc, TelegramNetworkError) and exc.ambiguous:
                    operator_console.finish_notification(
                        row, status=operator_console.OperatorNotification.Status.UNCERTAIN,
                        error=exc,
                    )
                    continue
                operator_console.retry_notification(row, exc)
                continue
            operator_console.finish_notification(
                row, status=operator_console.OperatorNotification.Status.SENT,
                external_id=(result or {}).get("message_id", ""),
            )
        return len(rows)

    def has_due_work(self) -> bool:
        now = timezone.now()
        pending = TelegramDeliveryStatus.PENDING
        return (
            TelegramOutboxEvent.objects.filter(
                status=TelegramOutboxEvent.Status.PENDING, next_attempt_at__lte=now
            ).exists()
            or TelegramMessage.objects.filter(
                delivery_status=pending, next_attempt_at__lte=now
            ).exists()
            or TelegramDelivery.objects.filter(status=pending, next_attempt_at__lte=now).exists()
            or MaxOperatorDelivery.objects.filter(
                status=pending, next_attempt_at__lte=now
            ).exists()
            or (
                operator_console.enabled()
                and OperatorNotification.objects.filter(
                    binding__provider="telegram",
                    status=OperatorNotification.Status.PENDING,
                    next_attempt_at__lte=now,
                ).exists()
            )
        )

    def drain_outbox(self) -> None:
        self.dispatch_events()
        self.send_customer_messages()
        self.send_operator_deliveries()
        self.send_max_operator_deliveries()
        if operator_console.enabled():
            runtime = operator_console.ensure_runtime()
            operator_console.queue_operator_notifications(
                since=runtime.announce_requests_since
            )
            self.send_operator_console_notifications()

    # Main loop ---------------------------------------------------------------------------

    def start(self) -> None:
        self.acquire()
        operator_console.invalidate_contexts("telegram")
        recovered = self.recover_interrupted_sends()
        if operator_console.enabled():
            recovered += operator_console.recover_interrupted_notifications("telegram")
        if recovered:
            logger.warning("marked %s interrupted sends as uncertain", recovered)
        webhook = self.api.get_webhook_info() or {}
        if webhook.get("url"):
            raise SingleInstanceError(
                "У бота настроен webhook: long polling невозможен. Удалите webhook (deleteWebhook)."
            )
        me = self.api.get_me() or {}
        TelegramBotRuntime.objects.filter(pk=TelegramBotRuntime.SINGLETON_PK).update(
            bot_username=str(me.get("username") or "")[:64]
        )
        self._touch_heartbeat()

    def start_with_retry(self) -> bool:
        """Start once Telegram answers; an outage waits here, holding the lease.

        Returns False only when a stop was requested while waiting. Telegram
        refusing the token or the request, a configured webhook and another
        consumer are not outages: they raise ``SingleInstanceError`` at once.
        """
        attempt = 0
        while not self.stop.is_set():
            try:
                self.start()
                if attempt:
                    logger.info("telegram reachable after %s startup attempts", attempt + 1)
                return True
            except TelegramApiError as exc:
                if not exc.retryable:
                    raise SingleInstanceError(str(exc)) from None
                error = exc
            except TelegramNetworkError as exc:
                error = exc
            attempt += 1
            delay = startup_backoff_seconds(attempt)
            retry_after = getattr(error, "retry_after", None)
            if retry_after:
                delay = min(max(delay, retry_after), STARTUP_BACKOFF_MAX_SECONDS)
            logger.warning(
                "telegram unavailable at startup (attempt %s): %s; next try in %ss",
                attempt, error, delay,
            )
            self.record_error(f"Старт: {error}")
            self._wait_holding_lease(delay)
        return False

    def _wait_holding_lease(self, seconds: float) -> None:
        """Sleep without giving up the single-consumer lease; a stop ends it at once."""
        remaining = float(seconds)
        while remaining > 0 and not self.stop.is_set():
            chunk = min(remaining, LEASE_RENEW_SLICE_SECONDS)
            if self.stop.wait(chunk):
                return
            remaining -= chunk
            self.renew()

    def iterate(self, poll_timeout: int | None = None) -> None:
        self.renew()
        if self._needs_recovery:
            # A database error interrupted a cycle. This single worker has no
            # send in flight now, so every row still marked ``sending`` stopped
            # between the claim and its result: never resend, show it.
            recovered = self.recover_interrupted_sends()
            if operator_console.enabled():
                recovered += operator_console.recover_interrupted_notifications("telegram")
            self._needs_recovery = False
            if recovered:
                logger.warning("marked %s interrupted sends as uncertain", recovered)
        timeout = self.poll_timeout if poll_timeout is None else poll_timeout
        self.poll_once(0 if self.has_due_work() else timeout)
        self.drain_outbox()
        self._touch_heartbeat()

    def run(self, *, once: bool = False) -> None:
        failures = 0
        try:
            # Inside try: a refused start (webhook set, bad token) must release
            # the lease at once, or a restart would wait for it to expire.
            if not self.start_with_retry():
                return
            while not self.stop.is_set():
                try:
                    self.iterate()
                    failures = 0
                except BusinessWriteBlocked:
                    logger.warning("business writes are blocked; bot paused")
                    self.stop.wait(30)
                except TelegramApiError as exc:
                    if exc.error_code in {401, 404, 409}:
                        # 409: another getUpdates consumer or a webhook; 401/404: bad token.
                        raise SingleInstanceError(str(exc)) from None
                    failures += 1
                    self.record_error(str(exc))
                    self.stop.wait(min(60, 2**failures))
                except TelegramError as exc:
                    failures += 1
                    logger.warning("telegram unavailable: %s", exc)
                    self.record_error(str(exc))
                    self.stop.wait(min(60, 2**failures))
                except DatabaseError as exc:
                    failures += 1
                    logger.error("database error: %s", type(exc).__name__)
                    self._needs_recovery = True
                    connection.close()
                    self.stop.wait(min(60, 2**failures))
                if once:
                    break
        finally:
            try:
                self.release()
            except DatabaseError:
                logger.warning("lease not released cleanly")
