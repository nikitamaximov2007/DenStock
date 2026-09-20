"""MAX request bot: webhook updates in, durable outbox out.

MAX delivers updates to a webhook; there is no offset to commit and no update
id. ``handle_update`` therefore runs inside the webhook's own transaction and
only stores things, each under a key a redelivery would find:

* a customer message by its string ``body.mid`` (unique);
* a bot answer by ``dedupe_key`` (``reply:<mid>``, ``callback:<press digest>``,
  ``start:<event digest>``, ``summary:<link token>:<n>``, ``ack:<conversation>``);
* a start by the link token it consumed: a repeated ``bot_started`` for a
  token this user already used is recognised and answered with silence.

``MaxBotWorker`` sends what was stored, one process at a time (PostgreSQL
advisory lock plus a lease row), with the same guarantees as the Telegram
worker: a row is marked ``sending`` in a committed transaction before the
network call; a send whose outcome is unknown becomes ``uncertain`` and is
never resent; refusals that can succeed later retry with bounded backoff.
MAX's limits are respected in-process: at most 2 messages a second into one
dialog and well under 30 requests a second overall.
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time
import uuid
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.db.models import F
from django.utils import timezone

from apps.customer_accounts import messenger_hooks as account_hooks
from apps.operations.models import MaxBotRuntime
from apps.operations.write_guard import BusinessWriteBlocked

from . import max_service as service
from . import messaging
from .attachments import AttachmentStorageError, cleanup_attachment, read_attachment
from .max_api import MaxApiError, MaxBotApi, MaxError, MaxNetworkError
from .messengers import MessengerLinkError, consume_max_start
from .models import (
    MaxDeliveryStatus,
    MaxMessage,
    MaxOperatorDelivery,
    MaxOutboxEvent,
)

logger = logging.getLogger("apps.customer_requests.max_bot")

UPDATE_TYPES = ("bot_started", "bot_stopped", "message_created", "message_callback")
ADVISORY_LOCK_ID = 0x44534D4158424F54 & 0x7FFFFFFFFFFFFFFF  # "DSMAXBOT"
LEASE_SECONDS = 90
MAX_ATTEMPTS = 8
EVENT_MAX_AGE = timedelta(days=7)
NO_OPERATOR_RETRY = timedelta(minutes=5)
BATCH = 20
IDLE_SECONDS = 1.0
STARTUP_BACKOFF_BASE_SECONDS = 5
STARTUP_BACKOFF_MAX_SECONDS = 300
LEASE_RENEW_SLICE_SECONDS = 30
# MAX: 2 messages per second into one dialog, 30 requests per second per bot.
# Both intervals keep a margin rather than riding the exact limit.
DIALOG_INTERVAL_SECONDS = 0.55
GLOBAL_INTERVAL_SECONDS = 1 / 25
RATE_LIMIT_PAUSE_MAX_SECONDS = 60


class SingleInstanceError(RuntimeError):
    """Another worker owns this bot, or MAX refuses the token itself."""


def backoff_seconds(attempts: int) -> int:
    return min(10 * 2 ** max(attempts - 1, 0), 900)


def startup_backoff_seconds(attempt: int) -> int:
    return min(
        STARTUP_BACKOFF_BASE_SECONDS * 2 ** max(attempt - 1, 0), STARTUP_BACKOFF_MAX_SECONDS
    )


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- Update handling (runs inside the webhook's transaction) ------------------------------


def _event_digest(*parts) -> str:
    """A stable identity for an update that carries none of its own.

    Built only from what MAX repeats in a redelivery. The start payload is a
    raw link token, so only its hash takes part; nothing reversible is stored.
    """
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode()).hexdigest()[:40]


def _start(*, token: str, user_id: int, chat_id: int, reply_key: str, user=None) -> None:
    # A website login link (``acc_…``) is an account event, not a request link.
    if account_hooks.max_start(
        payload=token, user_id=user_id, chat_id=chat_id, user=user, event_key=reply_key
    ):
        return
    try:
        # A savepoint: a refused link must leave the webhook transaction usable.
        with transaction.atomic():
            consume_max_start(token=token, chat_id=chat_id, user_id=user_id)
    except MessengerLinkError:
        if service.start_is_replay(token=token, user_id=user_id):
            return
        service.queue_message(chat_id=chat_id, text=service.LINK_INVALID_TEXT,
                              dedupe_key=reply_key)


def handle_update(update) -> str:
    """Apply one webhook update. Returns a short outcome label for logs and tests."""
    if not isinstance(update, dict):
        return "ignored"
    kind = update.get("update_type")
    if kind == "bot_started":
        return _bot_started(update)
    if kind == "message_created":
        return _message_created(update)
    if kind == "message_callback":
        return _message_callback(update)
    return "ignored"


def _user_id(user) -> int | None:
    if not isinstance(user, dict) or user.get("is_bot") is True:
        return None
    value = user.get("user_id")
    return value if _is_int(value) else None


def _bot_started(update) -> str:
    chat_id, user_id = update.get("chat_id"), _user_id(update.get("user"))
    if not _is_int(chat_id) or user_id is None:
        return "ignored"
    payload = update.get("payload")
    digest = _event_digest(
        "bot_started",
        user_id,
        chat_id,
        update.get("timestamp"),
        hashlib.sha256(str(payload or "").encode()).hexdigest(),
    )
    reply_key = f"start:{digest}"
    if isinstance(payload, str) and payload.strip():
        _start(
            token=payload.strip(),
            user_id=user_id,
            chat_id=chat_id,
            reply_key=reply_key,
            user=update.get("user"),
        )
        return "start"
    if service.linked_conversations(user_id):
        # Started again after a stop: offer the requests this user already has.
        service.queue_selector(user_id=user_id, chat_id=chat_id, dedupe_key=reply_key)
        return "selector"
    service.queue_greeting(user_id=user_id, chat_id=chat_id, dedupe_key=reply_key)
    return "greeting"


def _message_created(update) -> str:
    message = update.get("message")
    if not isinstance(message, dict):
        return "ignored"
    recipient = message.get("recipient") if isinstance(message.get("recipient"), dict) else {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}
    user_id = _user_id(message.get("sender"))
    chat_id = recipient.get("chat_id")
    mid = body.get("mid")
    # Dialogs only: a group chat must never become a customer conversation.
    if recipient.get("chat_type") != "dialog" or user_id is None or not _is_int(chat_id):
        return "ignored"
    if not service.valid_external_id(mid):
        # Stored whole or not at all. Never truncated into another message's id.
        logger.warning("max message ignored: unusable mid (length %s)", len(str(mid or "")))
        return "ignored"
    reply_key = f"reply:{mid}"
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        service.queue_message(chat_id=chat_id, text=service.MEDIA_NOT_SUPPORTED_TEXT,
                              dedupe_key=reply_key)
        return "media"
    text = text.strip()
    if text.startswith("/"):
        head, _, argument = text.partition(" ")
        command, argument = head.lower(), argument.strip()
        if command == "/start" and argument:
            # A returning customer's deep link into an existing dialog arrives
            # as a message, not as ``bot_started``.
            _start(
                token=argument,
                user_id=user_id,
                chat_id=chat_id,
                reply_key=reply_key,
                user=message.get("sender"),
            )
            return "start"
        if command == "/requests":
            service.queue_selector(user_id=user_id, chat_id=chat_id, dedupe_key=reply_key)
            return "selector"
        service.queue_greeting(user_id=user_id, chat_id=chat_id, dedupe_key=reply_key)
        return "greeting"
    return service.record_customer_message(user_id=user_id, chat_id=chat_id, mid=mid, text=text)


def _message_callback(update) -> str:
    callback = update.get("callback")
    if not isinstance(callback, dict):
        return "ignored"
    callback_id = callback.get("callback_id")
    user_id = _user_id(callback.get("user"))
    payload = callback.get("payload")
    message = update.get("message") if isinstance(update.get("message"), dict) else {}
    recipient = message.get("recipient") if isinstance(message.get("recipient"), dict) else {}
    chat_id = recipient.get("chat_id")
    if (
        not isinstance(callback_id, str)
        or not 0 < len(callback_id) <= 256
        or user_id is None
        or not isinstance(payload, str)
        or len(payload) > 256
    ):
        return "ignored"
    if not _is_int(chat_id):
        # The keyboard message was deleted; the user's known dialog still works.
        from .models import MaxCustomerChat

        state = MaxCustomerChat.objects.filter(user_id=user_id).first()
        if state is None:
            return "ignored"
        chat_id = state.chat_id
    # MAX calls callback_id the keyboard's identifier: a second press on the
    # same keyboard may repeat it. The press is what MAX redelivers verbatim.
    press_key = _event_digest(
        "message_callback", callback_id, user_id, payload, callback.get("timestamp")
    )
    if service.is_menu_payload(payload):
        # «Мои заявки»: show what is open now, in the message that was pressed.
        service.queue_selector(
            user_id=user_id,
            chat_id=chat_id,
            dedupe_key=f"callback:{press_key}",
            callback_id=callback_id,
        )
        return "selector"
    if service.is_purchases_payload(payload):
        text, buttons = service.purchase_selector_view(user_id)
        service.queue_message(
            chat_id=chat_id,
            text=text,
            buttons=buttons,
            callback_id=callback_id,
            dedupe_key=f"callback:{press_key}",
            in_place=True,
        )
        return "purchases"
    kind, _, value = payload.partition(":")
    if kind in {"p", "r", "rc"}:
        if kind == "p":
            text, buttons = service.purchase_detail_view(user_id=user_id, sale_id=value)
        elif kind == "r":
            text, buttons = service.reorder_preview_view(user_id=user_id, sale_id=value)
        else:
            text, buttons = service.confirm_reorder_view(
                user_id=user_id, sale_id=value, callback_key=press_key
            )
        service.queue_message(
            chat_id=chat_id,
            text=text,
            buttons=buttons,
            callback_id=callback_id,
            dedupe_key=f"callback:{press_key}",
            in_place=True,
        )
        return kind
    conversation = service.select_customer_conversation(
        user_id=user_id,
        chat_id=chat_id,
        payload=payload,
        callback_id=callback_id,
        press_key=press_key,
    )
    return "selected" if conversation is not None else "denied"


# --- Pacing ------------------------------------------------------------------------------


class SendPacer:
    """In-process spacing of API calls; one worker makes this sufficient."""

    def __init__(self, *, dialog_interval=DIALOG_INTERVAL_SECONDS,
                 global_interval=GLOBAL_INTERVAL_SECONDS, clock=time.monotonic, sleep=None):
        self.dialog_interval = dialog_interval
        self.global_interval = global_interval
        self.clock = clock
        self.sleep = sleep or time.sleep
        self._last_any = float("-inf")
        self._last_dialog: dict[int, float] = {}
        self._paused_until = float("-inf")

    def wait(self, chat_id: int | None) -> None:
        now = self.clock()
        ready = max(self._last_any + self.global_interval, self._paused_until)
        if chat_id is not None:
            ready = max(ready, self._last_dialog.get(chat_id, float("-inf")) + self.dialog_interval)
        if ready > now:
            self.sleep(ready - now)
            now = self.clock()
        self._last_any = now
        if chat_id is not None:
            self._last_dialog[chat_id] = now
            if len(self._last_dialog) > 1000:
                horizon = now - self.dialog_interval
                self._last_dialog = {k: v for k, v in self._last_dialog.items() if v > horizon}

    def pause(self, seconds: float) -> None:
        """MAX said 429: every call waits, bounded, not only the refused one."""
        seconds = min(max(float(seconds), 1.0), RATE_LIMIT_PAUSE_MAX_SECONDS)
        self._paused_until = max(self._paused_until, self.clock() + seconds)


# --- Worker ------------------------------------------------------------------------------


def build_api() -> MaxBotApi:
    return MaxBotApi(
        settings.MAX_BOT_TOKEN,
        base_url=settings.MAX_API_BASE_URL,
        timeout=settings.MAX_API_TIMEOUT_SECONDS,
        ca_file=settings.MAX_API_CA_FILE,
        ca_sha256=settings.MAX_API_CA_SHA256,
    )


class MaxBotWorker:
    def __init__(self, api, *, stop: threading.Event | None = None, worker_id: str = "",
                 heartbeat_file: str | None = None, pacer: SendPacer | None = None):
        self.api = api
        self.stop = stop or threading.Event()
        self.worker_id = worker_id or uuid.uuid4().hex
        self.heartbeat_file = (
            settings.MAX_BOT_HEARTBEAT_FILE if heartbeat_file is None else heartbeat_file
        )
        self.pacer = pacer or SendPacer(sleep=self._sleep)
        self._holds_lock = False
        self._needs_recovery = False

    def _sleep(self, seconds: float) -> None:
        self.stop.wait(seconds)

    # Single instance -------------------------------------------------------------------

    def acquire(self) -> None:
        self._lock_database()
        now = timezone.now()
        with transaction.atomic():
            runtime, _ = MaxBotRuntime.objects.select_for_update().get_or_create(
                pk=MaxBotRuntime.SINGLETON_PK
            )
            if (
                runtime.worker_id
                and runtime.worker_id != self.worker_id
                and runtime.lease_expires_at
                and runtime.lease_expires_at > now
            ):
                raise SingleInstanceError(
                    "Другой экземпляр MAX-бота уже работает (аренда не истекла)."
                )
            runtime.worker_id = self.worker_id
            runtime.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            runtime.started_at = runtime.heartbeat_at = now
            if runtime.announce_requests_since is None:
                runtime.announce_requests_since = now
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
                raise SingleInstanceError("Другой экземпляр MAX-бота держит блокировку.")
        self._holds_lock = True

    def renew(self) -> None:
        now = timezone.now()
        renewed = MaxBotRuntime.objects.filter(
            pk=MaxBotRuntime.SINGLETON_PK, worker_id=self.worker_id
        ).update(heartbeat_at=now, lease_expires_at=now + timedelta(seconds=LEASE_SECONDS))
        if not renewed:
            raise SingleInstanceError("Аренду MAX-бота перехватил другой экземпляр.")
        self._lock_database()

    def release(self) -> None:
        MaxBotRuntime.objects.filter(
            pk=MaxBotRuntime.SINGLETON_PK, worker_id=self.worker_id
        ).update(worker_id="", lease_expires_at=None)
        if self._holds_lock and connection.vendor == "postgresql":
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [ADVISORY_LOCK_ID])
            self._holds_lock = False

    def record_error(self, text: str) -> None:
        MaxBotRuntime.objects.filter(pk=MaxBotRuntime.SINGLETON_PK).update(
            last_error=str(text)[:255], last_error_at=timezone.now()
        )

    def _touch_heartbeat(self) -> None:
        """Record this worker's id: the healthcheck matches it against the lease."""
        if self.heartbeat_file:
            path = Path(self.heartbeat_file)
            try:
                partial = path.with_name(path.name + ".tmp")
                partial.write_text(self.worker_id, encoding="ascii")
                partial.replace(path)
            except OSError:
                logger.warning("heartbeat file is not writable")

    # Recovery ----------------------------------------------------------------------------

    def recover_interrupted_sends(self) -> int:
        note = "Отправка прервана остановкой бота; повторно не отправлялось."
        return MaxMessage.objects.filter(delivery_status=MaxDeliveryStatus.SENDING).update(
            delivery_status=MaxDeliveryStatus.UNCERTAIN, last_error=note
        )

    # Operator events ---------------------------------------------------------------------

    def _locked(self, queryset):
        return queryset.select_for_update(
            skip_locked=connection.features.has_select_for_update_skip_locked
        )

    def dispatch_events(self, limit: int = 50) -> int:
        """Fan one event out to every employee who should hear about it."""
        now = timezone.now()
        with transaction.atomic():
            events = list(
                self._locked(
                    MaxOutboxEvent.objects.filter(
                        status=MaxOutboxEvent.Status.PENDING, next_attempt_at__lte=now
                    )
                ).order_by("pk")[:limit]
            )
            for event in events:
                recipients = service.eligible_recipients(exclude_user_id=event.exclude_user_id)
                outcome = messaging.operator_event_outcome(
                    has_recipients=bool(recipients),
                    excludes_author=event.exclude_user_id is not None,
                    anyone_eligible=bool(recipients)
                    or (event.exclude_user_id is not None and bool(service.eligible_recipients())),
                    expired=now - event.created_at > EVENT_MAX_AGE,
                )
                if outcome == messaging.EVENT_DELIVER:
                    MaxOperatorDelivery.objects.bulk_create(
                        [
                            MaxOperatorDelivery(event=event, recipient=user, next_attempt_at=now)
                            for user in recipients
                        ],
                        ignore_conflicts=True,
                    )
                if outcome in (messaging.EVENT_DELIVER, messaging.EVENT_COMPLETE):
                    event.status = MaxOutboxEvent.Status.DISPATCHED
                    event.dispatched_at = now
                    event.save(update_fields=["status", "dispatched_at"])
                    continue
                if outcome == messaging.EVENT_EXPIRE:
                    event.status = MaxOutboxEvent.Status.EXPIRED
                event.attempts += 1
                event.next_attempt_at = now + NO_OPERATOR_RETRY
                event.save(update_fields=["status", "attempts", "next_attempt_at"])
        return len(events)

    # Customer sends ----------------------------------------------------------------------

    def _claim(self, limit: int) -> list[int]:
        """Due outgoing rows, never overtaking an earlier row still waiting in its dialog."""
        now = timezone.now()
        with transaction.atomic():
            candidates = list(
                self._locked(
                    MaxMessage.objects.filter(
                        delivery_status=MaxDeliveryStatus.PENDING, next_attempt_at__lte=now
                    ).exclude(direction=MaxMessage.Direction.CUSTOMER)
                )
                .order_by("pk")
                .values_list("pk", "recipient_chat_id")[: limit * 3]
            )
            chats = {chat for _pk, chat in candidates}
            waiting = list(
                MaxMessage.objects.filter(
                    delivery_status=MaxDeliveryStatus.PENDING,
                    next_attempt_at__gt=now,
                    recipient_chat_id__in=chats,
                ).values_list("pk", "recipient_chat_id", "next_attempt_at")
            )
            ids = []
            for pk, chat in candidates:
                earlier = [due for other, other_chat, due in waiting
                           if other_chat == chat and other < pk]
                if earlier:
                    # Held behind an earlier message of its dialog: it waits
                    # with it rather than staying due, or the loop would spin.
                    MaxMessage.objects.filter(pk=pk).update(next_attempt_at=min(earlier))
                elif len(ids) < limit:
                    ids.append(pk)
            MaxMessage.objects.filter(pk__in=ids).update(
                delivery_status=MaxDeliveryStatus.SENDING, attempts=F("attempts") + 1
            )
        return ids

    def _finish(self, row, status: str, *, mid=None, error="") -> None:
        values = {"delivery_status": status, "last_error": str(error)[:255]}
        if status == MaxDeliveryStatus.SENT:
            values.update(
                sent_at=timezone.now(),
                external_message_id=mid if service.valid_external_id(mid) else "",
            )
        # Use save() so the post_save signal emits a WorkspaceEvent and an
        # already-open operator workspace reconciles the delivery status.
        for field, value in values.items():
            setattr(row, field, value)
        update_fields = [*values, "updated_at"] if hasattr(row, "updated_at") else list(values)
        row.save(update_fields=update_fields)
        if status in (MaxDeliveryStatus.SENT, MaxDeliveryStatus.FAILED):
            cleanup_attachment(row)

    def _postpone(self, row, *, until, error="", count_attempt=True) -> None:
        updates = {
            "delivery_status": MaxDeliveryStatus.PENDING,
            "next_attempt_at": until,
            "last_error": str(error)[:255],
        }
        if not count_attempt:
            updates["attempts"] = F("attempts") - 1
        MaxMessage.objects.filter(pk=row.pk).update(**updates)
        # QuerySet.update() bypasses post_save, so the open workspace would
        # otherwise keep displaying the optimistic "Отправляется..." state.
        row.refresh_from_db(fields=["delivery_status", "next_attempt_at", "last_error", "attempts"])
        row.save(update_fields=["delivery_status", "next_attempt_at", "last_error", "attempts"])

    def _fail(self, row, exc: MaxError):
        """Record a failed send; returns when the dialog may continue, or None."""
        if isinstance(exc, MaxNetworkError) and exc.ambiguous:
            self._finish(row, MaxDeliveryStatus.UNCERTAIN, error=exc)
            return None
        retryable = isinstance(exc, MaxNetworkError) or (
            isinstance(exc, MaxApiError) and exc.retryable
        )
        if not retryable or row.attempts >= MAX_ATTEMPTS:
            self._finish(row, MaxDeliveryStatus.FAILED, error=exc)
            return None
        delay = getattr(exc, "retry_after", None) or backoff_seconds(row.attempts)
        if isinstance(exc, MaxApiError) and exc.status == 429:
            self.pacer.pause(getattr(exc, "retry_after", None) or 1)
        until = timezone.now() + timedelta(seconds=delay)
        self._postpone(row, until=until, error=exc)
        return until

    def send_customer_messages(self, limit: int = BATCH) -> int:
        ids = self._claim(limit)
        rows = MaxMessage.objects.select_related("conversation__request").filter(pk__in=ids)
        held: dict[int, object] = {}
        for row in rows.order_by("pk"):
            chat_id = row.recipient_chat_id
            if chat_id in held:
                # An earlier message of this dialog must retry first: keep order.
                self._postpone(row, until=held[chat_id], count_attempt=False)
                continue
            conversation = row.conversation
            if conversation is not None and (
                not conversation.is_linked
                or conversation.customer_chat_id != chat_id
                or not messaging.customer_contact_allowed(conversation.request)
            ):
                self._finish(row, MaxDeliveryStatus.FAILED,
                             error="Клиент недоступен для сообщений")
                continue
            if row.dedupe_key.startswith(service.SELECTOR_DEDUPE_PREFIX) and row.callback_id:
                # A selector the customer pressed: re-render that same message.
                if self._update_pressed_message(row):
                    continue
                # MAX refused the edit; fall through and send it as a message.
            elif row.callback_id:
                self._answer_callback(row)
            self.pacer.wait(chat_id)
            try:
                if row.attachment:
                    if row.max_attachment_token:
                        result = self.api.send_file_token(
                            chat_id=chat_id,
                            token=row.max_attachment_token,
                            content_type=row.attachment_content_type,
                            caption=row.text,
                        )
                    else:
                        content = read_attachment(row.attachment)
                        token = self.api.upload_file(
                            content=content,
                            filename=(
                                row.attachment_name
                                or row.attachment.name.rsplit("/", 1)[-1]
                            ),
                            content_type=row.attachment_content_type,
                        )
                        row.max_attachment_token = token
                        row.save(update_fields=["max_attachment_token"])
                        result = self.api.send_file_token(
                            chat_id=chat_id,
                            token=token,
                            content_type=row.attachment_content_type,
                            caption=row.text,
                        )
                else:
                    result = self.api.send_message(
                        chat_id=chat_id, text=row.text, buttons=row.buttons
                    )
            except AttachmentStorageError as exc:
                self._finish(row, MaxDeliveryStatus.FAILED, error=exc)
                continue
            except MaxApiError as exc:
                if exc.status == 401:
                    self._postpone(row, until=timezone.now(), error=exc, count_attempt=False)
                    for pending in rows.filter(pk__gt=row.pk):
                        self._postpone(pending, until=timezone.now(), count_attempt=False)
                    raise SingleInstanceError(str(exc)) from None
                until = self._fail(row, exc)
                if until is not None:
                    held[chat_id] = until
                continue
            except MaxError as exc:
                until = self._fail(row, exc)
                if until is not None:
                    held[chat_id] = until
                continue
            body = (result or {}).get("body") or {}
            self._finish(row, MaxDeliveryStatus.SENT, mid=body.get("mid"))
        return len(ids)

    def _update_pressed_message(self, row) -> bool:
        """Draw the selector in place; ``False`` means "send it as a message".

        Only the drawing can fail here: the customer's choice is already stored.
        """
        self.pacer.wait(None)
        try:
            self.api.answer_callback(
                callback_id=row.callback_id,
                message={"text": row.text, "buttons": row.buttons},
            )
        except MaxError as exc:
            logger.info("selector not updated in place: %s", exc)
            return False
        self._finish(row, MaxDeliveryStatus.SENT)
        return True

    def _answer_callback(self, row) -> None:
        """Stop the button's spinner. Harmless if lost: the message follows."""
        self.pacer.wait(None)
        try:
            self.api.answer_callback(callback_id=row.callback_id, notification="Готово")
        except MaxError as exc:
            logger.info("callback answer not sent: %s", exc)

    def purge_ephemeral(self) -> int:
        """Bot answers that belong to no request keep no identity for long."""
        horizon = timezone.now() - service.EPHEMERAL_RETENTION
        deleted, _ = MaxMessage.objects.filter(
            conversation__isnull=True,
            created_at__lt=horizon,
            delivery_status__in=[
                MaxDeliveryStatus.SENT, MaxDeliveryStatus.FAILED, MaxDeliveryStatus.UNCERTAIN,
                MaxDeliveryStatus.PENDING,
            ],
        ).delete()
        return deleted

    def has_due_work(self) -> bool:
        now = timezone.now()
        return (
            MaxOutboxEvent.objects.filter(
                status=MaxOutboxEvent.Status.PENDING, next_attempt_at__lte=now
            ).exists()
            or MaxMessage.objects.filter(
                delivery_status=MaxDeliveryStatus.PENDING, next_attempt_at__lte=now
            ).exists()
        )

    # Main loop ---------------------------------------------------------------------------

    def start(self) -> None:
        self.acquire()
        recovered = self.recover_interrupted_sends()
        if recovered:
            logger.warning("marked %s interrupted sends as uncertain", recovered)
        me = self.api.get_me()
        MaxBotRuntime.objects.filter(pk=MaxBotRuntime.SINGLETON_PK).update(
            bot_username=str(me.get("username") or "")[:64]
        )
        self._touch_heartbeat()

    def start_with_retry(self) -> bool:
        attempt = 0
        while not self.stop.is_set():
            try:
                self.start()
                return True
            except MaxApiError as exc:
                if not exc.retryable:
                    raise SingleInstanceError(str(exc)) from None
                error = exc
            except MaxNetworkError as exc:
                error = exc
            attempt += 1
            delay = startup_backoff_seconds(attempt)
            retry_after = getattr(error, "retry_after", None)
            if retry_after:
                delay = min(max(delay, retry_after), STARTUP_BACKOFF_MAX_SECONDS)
            logger.warning("max unavailable at startup (attempt %s): %s; next try in %ss",
                           attempt, error, delay)
            self.record_error(f"Старт: {error}")
            self._wait_holding_lease(delay)
        return False

    def _wait_holding_lease(self, seconds: float) -> None:
        remaining = float(seconds)
        while remaining > 0 and not self.stop.is_set():
            chunk = min(remaining, LEASE_RENEW_SLICE_SECONDS)
            if self.stop.wait(chunk):
                return
            remaining -= chunk
            self.renew()

    def iterate(self) -> None:
        self.renew()
        if self._needs_recovery:
            recovered = self.recover_interrupted_sends()
            self._needs_recovery = False
            if recovered:
                logger.warning("marked %s interrupted sends as uncertain", recovered)
        runtime = MaxBotRuntime.objects.get(pk=MaxBotRuntime.SINGLETON_PK)
        service.announce_new_requests(since=runtime.announce_requests_since)
        self.dispatch_events()
        self.send_customer_messages()
        self.purge_ephemeral()
        self._touch_heartbeat()

    def run(self, *, once: bool = False) -> None:
        failures = 0
        try:
            if not self.start_with_retry():
                return
            while not self.stop.is_set():
                try:
                    self.iterate()
                    failures = 0
                except BusinessWriteBlocked:
                    logger.warning("business writes are blocked; max bot paused")
                    self.stop.wait(30)
                except MaxError as exc:
                    failures += 1
                    logger.warning("max unavailable: %s", exc)
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
                if not self.has_due_work():
                    self.stop.wait(IDLE_SECONDS)
        finally:
            try:
                self.release()
            except DatabaseError:
                logger.warning("lease not released cleanly")


# --- Health ------------------------------------------------------------------------------

HEALTH_MAX_AGE_SECONDS = 90


def health_problems(heartbeat_file: str, *, now=None) -> list[str]:
    """Why this container's MAX worker is not healthy; empty means healthy.

    Three facts must agree: the process wrote its heartbeat recently, that
    heartbeat names a worker, and the database lease belongs to that worker and
    has not expired. Nothing here calls MAX or reads a secret.
    """
    import re
    import time

    path = Path(heartbeat_file or "")
    if not heartbeat_file or not path.is_file():
        return ["heartbeat file missing"]
    age = time.time() - path.stat().st_mtime
    if age > HEALTH_MAX_AGE_SECONDS:
        return [f"heartbeat stale ({int(age)} s)"]
    try:
        worker_id = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError):
        worker_id = ""
    if not re.fullmatch(r"[0-9a-f]{32}", worker_id):
        return ["heartbeat names no worker"]
    now = now or timezone.now()
    runtime = MaxBotRuntime.objects.filter(pk=MaxBotRuntime.SINGLETON_PK).first()
    if runtime is None:
        return ["runtime row missing"]
    problems = []
    if runtime.worker_id != worker_id:
        problems.append("lease belongs to another worker")
    if runtime.lease_expires_at is None or runtime.lease_expires_at <= now:
        problems.append("lease expired")
    if runtime.heartbeat_at is None or (now - runtime.heartbeat_at).total_seconds() > (
        HEALTH_MAX_AGE_SECONDS
    ):
        problems.append("runtime heartbeat stale")
    return problems
