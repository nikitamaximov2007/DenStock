"""Persistent, non-reserving customer requests.

A request is intentionally not a sale, a reservation, or a customer record.
It preserves the contact and the current customer-facing price observed at
submission, while all stock truth continues to live in inventory.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Value

from apps.core.phones import normalize_phone

from .storage import PrivateAttachmentStorage


class CustomerRequest(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        COMPLETED = "completed", "Выполнена"
        CANCELED = "canceled", "Отменена"

    class Messenger(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"

    class Source(models.TextChoices):
        PUBLIC_CATALOG = "public_catalog", "Публичный каталог"
        MESSENGER_REPEAT = "messenger_repeat", "Повтор покупки через мессенджер"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    human_number = models.PositiveBigIntegerField(
        "Номер заявки", unique=True, null=True, editable=False
    )
    status = models.CharField("Статус", max_length=20, choices=Status.choices, default=Status.NEW)
    source = models.CharField(
        "Источник", max_length=30, choices=Source.choices, default=Source.PUBLIC_CATALOG
    )
    customer_name = models.CharField("Имя", max_length=255)
    customer_phone = models.CharField("Телефон", max_length=50)
    customer_phone_normalized = models.CharField(
        "Телефон для поиска", max_length=50, editable=False, db_index=True
    )
    preferred_messenger = models.CharField(
        "Предпочтительный мессенджер", max_length=12, choices=Messenger.choices
    )
    comment = models.TextField("Комментарий", max_length=2000, blank=True)
    # Versions are immutable acceptance evidence. Their wording and legal
    # validity are configured/reviewed separately, never reconstructed from a
    # mutable page at a later date.
    privacy_policy_version = models.CharField("Версия политики", max_length=64)
    personal_data_consent_version = models.CharField("Версия согласия", max_length=64)
    consent_purpose = models.CharField("Цель согласия", max_length=120)
    consent_accepted_at = models.DateTimeField("Согласие принято")
    consent_withdrawn_at = models.DateTimeField("Согласие отозвано", null=True, blank=True)
    data_anonymized_at = models.DateTimeField("Данные обезличены", null=True, blank=True)
    # SHA-256 of an anonymous browser idempotency value. The raw value never
    # becomes business data and cannot be retrieved after submission.
    submission_key_hash = models.CharField("Ключ идемпотентности", max_length=64, unique=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The PRO-STOR account the request belongs to: set from a signed-in
    # customer's session, or at messenger handoff from the verified identity.
    # It complements messenger ownership and never replaces it: routing and
    # «Мои заявки» still check the conversation's own bound identity.
    customer_account = models.ForeignKey(
        "customer_accounts.CustomerAccount",
        verbose_name="Кабинет клиента",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="requests",
    )
    current_responder_label = models.CharField("Текущий ответственный", max_length=80, blank=True)
    current_responder_control_source = models.CharField(
        "Канал текущего ответственного", max_length=12, blank=True
    )
    pending_responder_label = models.CharField("Ожидаемый ответственный", max_length=80, blank=True)
    pending_responder_control_source = models.CharField(
        "Канал ожидаемого ответственного", max_length=12, blank=True
    )

    class Meta:
        verbose_name = "Заявка клиента"
        verbose_name_plural = "Заявки клиентов"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="custreq_status_created_idx"),
        ]

    def __str__(self) -> str:
        return f"Заявка №{self.reference} ({self.customer_name})"

    def save(self, *args, **kwargs):
        self.customer_name = (self.customer_name or "").strip()
        self.customer_phone = (self.customer_phone or "").strip()
        self.customer_phone_normalized = normalize_phone(self.customer_phone)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "customer_phone" in update_fields:
            kwargs["update_fields"] = sorted(set(update_fields) | {"customer_phone_normalized"})
        super().save(*args, **kwargs)

    @staticmethod
    def reference_for(public_id) -> str:
        """Short number shown to the customer and to the operator, e.g. ``5F3A9C21``."""
        return str(public_id).split("-", 1)[0].upper()

    @property
    def reference(self) -> str:
        return (
            str(self.human_number)
            if self.human_number is not None
            else self.reference_for(self.public_id)
        )


class CustomerRequestNumberSequence(models.Model):
    """A single locked counter; numbers are never derived from MAX()."""

    singleton = models.BooleanField(default=True, unique=True, editable=False)
    next_number = models.PositiveBigIntegerField(default=1)

    class Meta:
        verbose_name = "Последовательность номеров заявок"

    def __str__(self):
        return f"Следующий номер: {self.next_number}"


class WorkspaceEvent(models.Model):
    """Small durable cursor log for the operator request workspace."""

    event_id = models.BigAutoField(primary_key=True)
    event_type = models.CharField("Тип", max_length=64)
    request = models.ForeignKey(
        CustomerRequest,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="workspace_events",
    )
    entity_type = models.CharField("Тип объекта", max_length=64)
    entity_id = models.CharField("Идентификатор объекта", max_length=128)
    payload = models.JSONField("Данные", default=dict)
    created_at = models.DateTimeField("Создано", auto_now_add=True)

    class Meta:
        ordering = ["event_id"]
        indexes = [models.Index(fields=["event_id"], name="workspace_event_cursor_idx")]

    def __str__(self):
        return f"{self.event_type} #{self.event_id}"


class CustomerRequestLine(models.Model):
    request = models.ForeignKey(
        CustomerRequest, verbose_name="Заявка", on_delete=models.CASCADE, related_name="lines"
    )
    part_type = models.ForeignKey(
        "catalog.PartType",
        verbose_name="Деталь",
        on_delete=models.PROTECT,
        related_name="customer_requests",
    )
    quantity_requested = models.DecimalField("Запрошено", max_digits=12, decimal_places=3)
    unit_name = models.CharField("Единица (снимок)", max_length=50)
    unit_short_name = models.CharField("Единица (сокращение, снимок)", max_length=20)
    # Null means that the current public price must be clarified. It is not a
    # zero price and never a guaranteed or contract price.
    price_seen = models.DecimalField(
        "Цена на момент заявки (₽)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    article = models.CharField("Артикул (снимок)", max_length=100, blank=True)
    part_name = models.CharField("Название (снимок)", max_length=200)
    is_supply_inquiry = models.BooleanField("Запрос о поставке", default=False)

    class Meta:
        verbose_name = "Позиция заявки"
        verbose_name_plural = "Позиции заявки"
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity_requested__gt=Decimal("0")),
                name="custreq_line_quantity_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(price_seen__isnull=True) | models.Q(price_seen__gte=Decimal("0"))
                ),
                name="custreq_line_price_nonnegative",
            ),
            models.UniqueConstraint(
                fields=["request", "part_type"], name="custreq_line_part_unique"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.part_name} × {self.quantity_requested}"


class CustomerRequestStatusEvent(models.Model):
    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="status_events",
    )
    from_status = models.CharField(
        "Статус до", max_length=20, choices=CustomerRequest.Status.choices
    )
    to_status = models.CharField(
        "Статус после", max_length=20, choices=CustomerRequest.Status.choices
    )
    changed_at = models.DateTimeField("Изменён", auto_now_add=True)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто изменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Изменение статуса заявки"
        verbose_name_plural = "Изменения статуса заявок"
        ordering = ["-changed_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_from_status_display()} → {self.get_to_status_display()}"


class CustomerRequestMessengerContact(models.Model):
    """Minimal channel identity, recorded only after the user starts the bot."""

    class Channel(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"

    request = models.OneToOneField(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="messenger_contact",
    )
    channel = models.CharField("Канал", max_length=12, choices=Channel.choices)
    remote_chat_id = models.CharField("Идентификатор чата", max_length=64)
    linked_at = models.DateTimeField("Связан", auto_now_add=True)

    class Meta:
        verbose_name = "Контакт заявки в мессенджере"
        verbose_name_plural = "Контакты заявок в мессенджерах"
        # One Telegram account may start the bot for several of its own
        # requests, so a chat is no longer unique across requests. Routing
        # between them lives in TelegramCustomerChat.

    def __str__(self) -> str:
        return f"{self.get_channel_display()} для заявки {self.request_id}"


class CustomerRequestMessengerLinkToken(models.Model):
    """One-time secret link, stored only as a SHA-256 hash."""

    class Channel(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"

    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="messenger_link_tokens",
    )
    channel = models.CharField("Канал", max_length=12, choices=Channel.choices)
    token_hash = models.CharField("Хеш токена", max_length=64, unique=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    expires_at = models.DateTimeField("Действует до")
    used_at = models.DateTimeField("Использован", null=True, blank=True)
    revoked_at = models.DateTimeField("Отозван", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Ссылка заявки в мессенджер"
        verbose_name_plural = "Ссылки заявок в мессенджеры"
        indexes = [
            models.Index(fields=["channel", "expires_at"], name="custreq_link_channel_exp_idx")
        ]

    def __str__(self) -> str:
        return f"{self.get_channel_display()} ссылка для заявки {self.request_id}"


class CustomerRequestPrivacyEvent(models.Model):
    class EventType(models.TextChoices):
        CONSENT_WITHDRAWN = "consent_withdrawn", "Согласие отозвано"
        ANONYMIZED = "anonymized", "Данные обезличены"

    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="privacy_events",
    )
    event_type = models.CharField("Событие", max_length=24, choices=EventType.choices)
    occurred_at = models.DateTimeField("Произошло", auto_now_add=True)
    performed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто выполнил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Событие приватности заявки"
        verbose_name_plural = "События приватности заявок"
        ordering = ["-occurred_at", "-pk"]

    def __str__(self) -> str:
        return self.get_event_type_display()


# --- Telegram messaging ------------------------------------------------------------------
#
# CustomerRequest stays the source of truth. These rows only add the Telegram
# identity of the customer, the conversation history and a durable outbox, so
# a request never depends on Telegram being reachable.


class TelegramConversation(models.Model):
    class Status(models.TextChoices):
        AWAITING_LINK = "awaiting_link", "Ожидает подключения"
        LINKED = "linked", "Telegram подключён"
        CLOSED = "closed", "Закрыта"

    # Opaque identifier for bot buttons; the primary key is never exposed.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request = models.OneToOneField(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="telegram_conversation",
    )
    status = models.CharField(
        "Состояние", max_length=20, choices=Status.choices, default=Status.AWAITING_LINK
    )
    # Numeric Telegram identities are the only security identity. The
    # username is a display snapshot and authorizes nothing.
    customer_chat_id = models.BigIntegerField("Чат клиента", null=True, blank=True, db_index=True)
    customer_user_id = models.BigIntegerField("Telegram ID клиента", null=True, blank=True)
    customer_username = models.CharField("Имя пользователя (снимок)", max_length=64, blank=True)
    linked_at = models.DateTimeField("Подключён", null=True, blank=True)
    last_message_at = models.DateTimeField("Последнее сообщение", null=True, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    class Meta:
        verbose_name = "Переписка Telegram"
        verbose_name_plural = "Переписки Telegram"
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"Telegram по заявке {self.request_id}"

    @property
    def is_linked(self) -> bool:
        return self.status == self.Status.LINKED and self.customer_chat_id is not None


class TelegramCustomerChat(models.Model):
    """Which of a chat's own linked requests receives its next plain message."""

    chat_id = models.BigIntegerField("Чат клиента", unique=True)
    active_conversation = models.ForeignKey(
        TelegramConversation,
        verbose_name="Активная переписка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Чат клиента Telegram"
        verbose_name_plural = "Чаты клиентов Telegram"

    def __str__(self) -> str:
        return f"Чат {self.chat_id}"


class TelegramOperator(models.Model):
    """An employee allowed to use the bot, bound to an internal DenisStock user."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Администратор"
        OPERATOR = "operator", "Оператор"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        verbose_name="Пользователь DenisStock",
        on_delete=models.PROTECT,
        related_name="telegram_operator",
    )
    telegram_user_id = models.BigIntegerField("Telegram ID", unique=True)
    role = models.CharField("Роль", max_length=12, choices=Role.choices, default=Role.OPERATOR)
    is_active = models.BooleanField("Активен", default=True)
    # Superseded by ``reply_request``, which names a request of either
    # transport. Kept, unused, so the previous release can still be rolled back to.
    reply_conversation = models.ForeignKey(
        TelegramConversation,
        verbose_name="Отвечает на (Telegram, прежнее поле)",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The request the operator's next plain text answers, whatever messenger
    # its customer uses. Only the operator's own explicit choice sets it.
    reply_request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Отвечает на заявку",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    reply_started_at = models.DateTimeField("Начал ответ", null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто добавил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Сотрудник в Telegram-боте"
        verbose_name_plural = "Сотрудники в Telegram-боте"
        ordering = ["pk"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(telegram_user_id__gt=0), name="tg_operator_user_id_positive"
            )
        ]

    def __str__(self) -> str:
        return f"{self.user} ({self.telegram_user_id})"


class TelegramDeliveryStatus(models.TextChoices):
    RECEIVED = "received", "Получено"
    PENDING = "pending", "В очереди"
    SENDING = "sending", "Отправляется"
    SENT = "sent", "Отправлено"
    FAILED = "failed", "Не доставлено"
    # The process stopped or timed out mid-send: Telegram may or may not have
    # delivered it. Never resent automatically, so a customer never gets a
    # duplicate; visible to staff instead.
    UNCERTAIN = "uncertain", "Неизвестно, доставлено ли"


class TelegramMessage(models.Model):
    class Direction(models.TextChoices):
        CUSTOMER = "customer_to_operator", "Клиент"
        OPERATOR = "operator_to_customer", "Сотрудник"
        SYSTEM = "system", "Система"

    conversation = models.ForeignKey(
        TelegramConversation,
        verbose_name="Переписка",
        on_delete=models.CASCADE,
        related_name="messages",
    )
    direction = models.CharField("Направление", max_length=24, choices=Direction.choices)
    text = models.TextField("Текст", max_length=4096, blank=True)
    attachment = models.FileField(
        "Вложение",
        upload_to="customer_requests/",
        blank=True,
        storage=PrivateAttachmentStorage(),
    )
    attachment_name = models.CharField("Имя вложения", max_length=180, blank=True)
    attachment_content_type = models.CharField("Тип вложения", max_length=80, blank=True)
    delivery_status = models.CharField(
        "Доставка",
        max_length=12,
        choices=TelegramDeliveryStatus.choices,
        default=TelegramDeliveryStatus.RECEIVED,
    )
    telegram_message_id = models.BigIntegerField("Сообщение Telegram", null=True, blank=True)
    # Unique: a repeated Telegram update can never store a message twice.
    telegram_update_id = models.BigIntegerField(
        "Обновление Telegram", null=True, blank=True, unique=True
    )
    # One employee reply per submission of the DenisStock reply form. A reply
    # typed in the bot is already unique by its update id and keeps this empty.
    # ``db_default`` keeps inserts by the previous release valid, so this
    # migration can be deployed before its code and rolled back after it.
    dedupe_key = models.CharField(
        "Ключ сообщения", max_length=160, blank=True, default="", db_default=Value("")
    )
    operator = models.ForeignKey(
        TelegramOperator,
        verbose_name="Сотрудник в боте",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="messages",
    )
    # Audit of the real employee, kept even if the operator row changes later.
    operator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Сотрудник",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    operator_control_source = models.CharField("Канал управления", max_length=12, blank=True)
    operator_author_label = models.CharField("Подпись сотрудника", max_length=80, blank=True)
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "Сообщение Telegram"
        verbose_name_plural = "Сообщения Telegram"
        ordering = ["created_at", "pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="tg_message_dedupe_unique",
            ),
        ]
        indexes = [
            models.Index(
                fields=["delivery_status", "next_attempt_at"], name="tg_message_delivery_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_direction_display()} в переписке {self.conversation_id}"


class TelegramOutboxEvent(models.Model):
    """Something operators must hear about, stored in the same transaction."""

    class Kind(models.TextChoices):
        NEW_REQUEST = "new_request", "Новая заявка"
        CUSTOMER_LINKED = "customer_linked", "Клиент подключил Telegram"
        CUSTOMER_MESSAGE = "customer_message", "Сообщение клиента"
        OPERATOR_REPLY = "operator_reply", "Ответ сотрудника"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает рассылки"
        DISPATCHED = "dispatched", "Разослано сотрудникам"
        EXPIRED = "expired", "Истекло без сотрудников"

    kind = models.CharField("Событие", max_length=24, choices=Kind.choices)
    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="telegram_events",
    )
    message = models.ForeignKey(
        TelegramMessage,
        verbose_name="Сообщение",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )
    exclude_operator = models.ForeignKey(
        TelegramOperator,
        verbose_name="Не уведомлять",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # One event per fact, whatever retries or restarts happen.
    dedupe_key = models.CharField("Ключ события", max_length=120, unique=True)
    status = models.CharField(
        "Состояние", max_length=12, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    dispatched_at = models.DateTimeField("Разослано", null=True, blank=True)

    class Meta:
        verbose_name = "Событие Telegram для сотрудников"
        verbose_name_plural = "События Telegram для сотрудников"
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="tg_event_status_idx")
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} по заявке {self.request_id}"


class TelegramDelivery(models.Model):
    """One operator's copy of one event; unique so a restart cannot duplicate it."""

    event = models.ForeignKey(
        TelegramOutboxEvent,
        verbose_name="Событие",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    operator = models.ForeignKey(
        TelegramOperator,
        verbose_name="Сотрудник",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    status = models.CharField(
        "Доставка",
        max_length=12,
        choices=TelegramDeliveryStatus.choices,
        default=TelegramDeliveryStatus.PENDING,
    )
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    telegram_message_id = models.BigIntegerField("Сообщение Telegram", null=True, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "Уведомление сотрудника в Telegram"
        verbose_name_plural = "Уведомления сотрудников в Telegram"
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["event", "operator"], name="tg_delivery_unique")
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="tg_delivery_status_idx")
        ]

    def __str__(self) -> str:
        return f"Уведомление {self.event_id} для {self.operator_id}"


# --- MAX messaging -----------------------------------------------------------------------
#
# The MAX transport keeps its own rows, next to Telegram's rather than inside
# them: Telegram's proven tables and history stay untouched. The rules a
# customer notices are shared (``messaging``); what differs is identity. MAX
# numbers users and dialogs, but identifies a message only by the opaque
# string ``body.mid`` and has no update id at all.


class MaxDeliveryStatus(models.TextChoices):
    RECEIVED = "received", "Получено"
    PENDING = "pending", "В очереди"
    SENDING = "sending", "Отправляется"
    SENT = "sent", "Отправлено"
    FAILED = "failed", "Не доставлено"
    # MAX may or may not have delivered it. Never resent automatically.
    UNCERTAIN = "uncertain", "Неизвестно, доставлено ли"


# A MAX ``mid`` is stored whole. A longer one is refused, never truncated:
# two different messages must never collapse into one identity.
MAX_EXTERNAL_ID_LENGTH = 512


class MaxConversation(models.Model):
    class Status(models.TextChoices):
        AWAITING_LINK = "awaiting_link", "Ожидает подключения"
        LINKED = "linked", "MAX подключён"
        CLOSED = "closed", "Закрыта"

    # Opaque identifier for bot buttons; the primary key is never exposed.
    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    request = models.OneToOneField(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="max_conversation",
    )
    status = models.CharField(
        "Состояние", max_length=20, choices=Status.choices, default=Status.AWAITING_LINK
    )
    # The MAX user is the security identity; the dialog is only where to send.
    customer_user_id = models.BigIntegerField(
        "MAX ID клиента", null=True, blank=True, db_index=True
    )
    customer_chat_id = models.BigIntegerField("Диалог клиента", null=True, blank=True)
    linked_at = models.DateTimeField("Подключён", null=True, blank=True)
    last_message_at = models.DateTimeField("Последнее сообщение", null=True, blank=True)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)

    class Meta:
        verbose_name = "Переписка MAX"
        verbose_name_plural = "Переписки MAX"
        ordering = ["-created_at", "-pk"]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(status="linked")
                | (
                    models.Q(customer_user_id__isnull=False)
                    & models.Q(customer_chat_id__isnull=False)
                ),
                name="max_conversation_linked_identity",
            )
        ]

    def __str__(self) -> str:
        return f"MAX по заявке {self.request_id}"

    @property
    def is_linked(self) -> bool:
        return (
            self.status == self.Status.LINKED
            and self.customer_user_id is not None
            and self.customer_chat_id is not None
        )


class MaxCustomerChat(models.Model):
    """One MAX user: their dialog with the bot and the request plain text goes to."""

    user_id = models.BigIntegerField("MAX ID клиента", unique=True)
    chat_id = models.BigIntegerField("Диалог клиента")
    active_conversation = models.ForeignKey(
        MaxConversation,
        verbose_name="Активная переписка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Клиент MAX"
        verbose_name_plural = "Клиенты MAX"

    def __str__(self) -> str:
        return f"Клиент MAX {self.user_id}"


class MaxMessage(models.Model):
    class Direction(models.TextChoices):
        CUSTOMER = "customer_to_operator", "Клиент"
        OPERATOR = "operator_to_customer", "Сотрудник"
        SYSTEM = "system", "Система"

    # Empty only for a bot answer that belongs to no request yet: a greeting,
    # an invalid link, or the question which request a message is for.
    conversation = models.ForeignKey(
        MaxConversation,
        verbose_name="Переписка",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="messages",
    )
    direction = models.CharField("Направление", max_length=24, choices=Direction.choices)
    text = models.TextField("Текст", max_length=4000, blank=True)
    attachment = models.FileField(
        "Вложение",
        upload_to="customer_requests/",
        blank=True,
        storage=PrivateAttachmentStorage(),
    )
    attachment_name = models.CharField("Имя вложения", max_length=180, blank=True)
    attachment_content_type = models.CharField("Тип вложения", max_length=80, blank=True)
    # MAX processes uploaded files asynchronously. Keep the token so a retry
    # can send the same upload instead of creating a fresh, not-yet-ready file.
    max_attachment_token = models.CharField("Токен вложения MAX", max_length=512, blank=True)
    # Callback buttons of a bot message: [[{"text": ..., "payload": ...}], ...].
    buttons = models.JSONField("Кнопки", null=True, blank=True)
    delivery_status = models.CharField(
        "Доставка",
        max_length=12,
        choices=MaxDeliveryStatus.choices,
        default=MaxDeliveryStatus.RECEIVED,
    )
    # Where an outgoing message goes. Incoming messages keep none.
    recipient_chat_id = models.BigIntegerField("Диалог получателя", null=True, blank=True)
    # MAX ``body.mid``: of the customer's message when received, of ours when sent.
    external_message_id = models.CharField(
        "Сообщение MAX", max_length=MAX_EXTERNAL_ID_LENGTH, blank=True
    )
    # One outgoing message per fact (a summary part, an acknowledgement, a
    # reply form submission): a replayed webhook or a double click finds it.
    dedupe_key = models.CharField("Ключ сообщения", max_length=160, blank=True)
    # The button press this message answers, acknowledged to MAX when sent.
    callback_id = models.CharField("Нажатие кнопки", max_length=256, blank=True)
    operator_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Сотрудник",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    operator_control_source = models.CharField("Канал управления", max_length=12, blank=True)
    operator_author_label = models.CharField("Подпись сотрудника", max_length=80, blank=True)
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "Сообщение MAX"
        verbose_name_plural = "Сообщения MAX"
        ordering = ["created_at", "pk"]
        constraints = [
            # A redelivered webhook can never store a customer message twice.
            models.UniqueConstraint(
                fields=["external_message_id"],
                condition=models.Q(direction="customer_to_operator"),
                name="max_message_inbound_mid_unique",
            ),
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=~models.Q(dedupe_key=""),
                name="max_message_dedupe_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(direction="customer_to_operator")
                | (models.Q(conversation__isnull=False) & ~models.Q(external_message_id="")),
                name="max_message_inbound_has_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(direction="customer_to_operator")
                | models.Q(recipient_chat_id__isnull=False),
                name="max_message_outbound_has_recipient",
            ),
        ]
        indexes = [
            models.Index(
                fields=["delivery_status", "next_attempt_at"], name="max_message_delivery_idx"
            )
        ]

    def __str__(self) -> str:
        return f"{self.get_direction_display()} MAX {self.pk}"


class MaxOutboxEvent(models.Model):
    """Something operators must hear about, stored in the same transaction."""

    class Kind(models.TextChoices):
        NEW_REQUEST = "new_request", "Новая заявка"
        CUSTOMER_LINKED = "customer_linked", "Клиент подключил MAX"
        CUSTOMER_MESSAGE = "customer_message", "Сообщение клиента"
        OPERATOR_REPLY = "operator_reply", "Ответ сотрудника"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает рассылки"
        DISPATCHED = "dispatched", "Разослано сотрудникам"
        EXPIRED = "expired", "Истекло без сотрудников"

    kind = models.CharField("Событие", max_length=24, choices=Kind.choices)
    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        related_name="max_events",
    )
    message = models.ForeignKey(
        MaxMessage,
        verbose_name="Сообщение",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="events",
    )
    # The DenisStock employee whose own action this is: never told about it.
    exclude_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Не уведомлять",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    dedupe_key = models.CharField("Ключ события", max_length=120, unique=True)
    status = models.CharField(
        "Состояние", max_length=12, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    dispatched_at = models.DateTimeField("Разослано", null=True, blank=True)

    class Meta:
        verbose_name = "Событие MAX для сотрудников"
        verbose_name_plural = "События MAX для сотрудников"
        ordering = ["pk"]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="max_event_status_idx")
        ]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} по заявке {self.request_id}"


class MaxOperatorDelivery(models.Model):
    """One employee's copy of one MAX event.

    The recipient is the DenisStock user, not a messenger account: employees
    work from DenisStock and are reached through the operators' existing
    notification bot. Unique, so a restart cannot notify twice.
    """

    event = models.ForeignKey(
        MaxOutboxEvent,
        verbose_name="Событие",
        on_delete=models.CASCADE,
        related_name="deliveries",
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Сотрудник",
        on_delete=models.CASCADE,
        related_name="+",
    )
    status = models.CharField(
        "Доставка",
        max_length=12,
        choices=MaxDeliveryStatus.choices,
        default=MaxDeliveryStatus.PENDING,
    )
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    telegram_message_id = models.BigIntegerField("Сообщение в боте", null=True, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "Уведомление сотрудника о MAX"
        verbose_name_plural = "Уведомления сотрудников о MAX"
        ordering = ["pk"]
        constraints = [
            models.UniqueConstraint(fields=["event", "recipient"], name="max_delivery_unique")
        ]
        indexes = [
            models.Index(fields=["status", "next_attempt_at"], name="max_delivery_status_idx")
        ]

    def __str__(self) -> str:
        return f"Уведомление {self.event_id} для {self.recipient_id}"


class StaffMessengerBinding(models.Model):
    """Explicit, revocable binding of one provider identity to one employee."""

    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Сотрудник DenisStock",
        on_delete=models.PROTECT,
        related_name="staff_messenger_bindings",
    )
    operator_key = models.CharField(
        "Ключ личности оператора", max_length=32, default="", blank=True
    )
    provider = models.CharField("Мессенджер", max_length=12, choices=Provider.choices)
    provider_user_id = models.BigIntegerField("ID пользователя мессенджера")
    delivery_chat_id = models.BigIntegerField(
        "Диалог для уведомлений", null=True, blank=True
    )
    customer_visible_label = models.CharField("Подпись для клиента", max_length=80)
    is_active = models.BooleanField("Активна", default=True)
    operator_mode = models.BooleanField("Рабочий режим", default=False)
    created_at = models.DateTimeField("Создана", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлена", auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто привязал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_staff_messenger_bindings",
    )

    class Meta:
        verbose_name = "Привязка сотрудника к мессенджеру"
        verbose_name_plural = "Привязки сотрудников к мессенджерам"
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_user_id"], name="staff_binding_provider_user_unique"
            ),
            models.UniqueConstraint(
                fields=["user", "provider", "operator_key"],
                name="staff_binding_user_provider_key_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(provider_user_id__gt=0),
                name="staff_binding_provider_id_positive",
            ),
        ]
        indexes = [models.Index(fields=["provider", "is_active"], name="staff_binding_active_idx")]

    def __str__(self):
        return f"{self.user} · {self.get_provider_display()}"


class StaffMessengerPairingToken(models.Model):
    """One-time, hashed pairing invitation started by an authenticated admin."""

    class Provider(models.TextChoices):
        TELEGRAM = "telegram", "Telegram"
        MAX = "max", "MAX"

    token_hash = models.CharField("Хеш токена", max_length=64, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Сотрудник DenisStock",
        on_delete=models.PROTECT,
        related_name="staff_pairing_tokens",
    )
    operator_key = models.CharField(
        "Ключ личности оператора", max_length=32, default="", blank=True
    )
    provider = models.CharField(
        "Мессенджер", max_length=12, choices=Provider.choices, blank=True, default=""
    )
    customer_visible_label = models.CharField("Подпись для клиента", max_length=80)
    expires_at = models.DateTimeField("Истекает")
    telegram_consumed_at = models.DateTimeField(
        "Telegram подключён", null=True, blank=True
    )
    max_consumed_at = models.DateTimeField("MAX подключён", null=True, blank=True)
    # Kept for backwards-compatible reading of tokens created by the original
    # provider-specific implementation. New codes use the two slot fields.
    used_at = models.DateTimeField("Использован", null=True, blank=True)
    revoked_at = models.DateTimeField("Отозван", null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто создал",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_staff_pairing_tokens",
    )

    class Meta:
        verbose_name = "Одноразовый код привязки сотрудника"
        verbose_name_plural = "Одноразовые коды привязки сотрудников"
        indexes = [models.Index(fields=["provider", "expires_at"], name="staff_pairing_exp_idx")]

    def __str__(self):
        return f"{self.get_provider_display()} · {self.user} · {self.expires_at:%Y-%m-%d %H:%M}"


class OperatorConversationContext(models.Model):
    """The request currently selected by one provider identity."""

    binding = models.OneToOneField(
        StaffMessengerBinding,
        verbose_name="Привязка",
        on_delete=models.CASCADE,
        related_name="context",
    )
    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Активная заявка",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="operator_contexts",
    )
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    def __str__(self):
        return f"{self.binding} · {self.request or 'без заявки'}"


class OwnerPhotoUploadContext(models.Model):
    """Short-lived, explicit PartType target for the next owner photo."""

    binding = models.OneToOneField(
        StaffMessengerBinding,
        verbose_name="Привязка владельца",
        on_delete=models.CASCADE,
        related_name="photo_upload_context",
    )
    part_type = models.ForeignKey(
        "catalog.PartType",
        verbose_name="Деталь",
        on_delete=models.PROTECT,
        related_name="owner_photo_contexts",
    )
    operation_type = models.CharField("Тип операции", max_length=12)
    operation_id = models.PositiveBigIntegerField("Операция")
    article_snapshot = models.CharField("Артикул (снимок)", max_length=100, blank=True)
    part_name_snapshot = models.CharField("Название (снимок)", max_length=200)
    expires_at = models.DateTimeField("Истекает")
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)

    class Meta:
        verbose_name = "Контекст загрузки фото владельца"
        verbose_name_plural = "Контексты загрузки фото владельца"
        indexes = [
            models.Index(fields=["expires_at"], name="owner_photo_ctx_exp_idx"),
        ]

    def __str__(self):
        return f"{self.binding} · {self.part_type}"


class OwnerPhotoUploadReceipt(models.Model):
    """Idempotency record for one messenger update handled as a photo upload."""

    binding = models.ForeignKey(
        StaffMessengerBinding,
        verbose_name="Привязка владельца",
        on_delete=models.CASCADE,
        related_name="photo_upload_receipts",
    )
    external_id = models.CharField("Идентификатор обновления", max_length=160)
    part_type = models.ForeignKey(
        "catalog.PartType",
        verbose_name="Деталь",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owner_photo_receipts",
    )
    response_text = models.TextField("Ответ")
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        verbose_name = "Квитанция загрузки фото владельца"
        verbose_name_plural = "Квитанции загрузки фото владельца"
        constraints = [
            models.UniqueConstraint(
                fields=["binding", "external_id"], name="owner_photo_receipt_unique"
            )
        ]

    def __str__(self):
        return f"{self.binding} · {self.external_id}"


class OperatorNotification(models.Model):
    """Durable, per-binding notification for the mobile operator workspace."""

    class Kind(models.TextChoices):
        NEW_REQUEST = "new_request", "Новая заявка"
        CUSTOMER_MESSAGE = "customer_message", "Сообщение клиента"
        OWNER_PANEL = "owner_panel", "Панель владельца"

    class Status(models.TextChoices):
        PENDING = "pending", "В очереди"
        SENDING = "sending", "Отправляется"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Не доставлено"
        UNCERTAIN = "uncertain", "Неизвестно, доставлено ли"

    binding = models.ForeignKey(
        StaffMessengerBinding,
        verbose_name="Привязка",
        on_delete=models.CASCADE,
        related_name="notifications",
    )
    request = models.ForeignKey(
        CustomerRequest,
        verbose_name="Заявка",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="operator_notifications",
    )
    kind = models.CharField("Событие", max_length=24, choices=Kind.choices)
    dedupe_key = models.CharField("Ключ идемпотентности", max_length=180, unique=True)
    preview = models.CharField("Краткий текст", max_length=700, blank=True)
    status = models.CharField(
        "Состояние", max_length=12, choices=Status.choices, default=Status.PENDING
    )
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    external_message_id = models.CharField("Внешний ID", max_length=512, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "Уведомление мобильной консоли"
        verbose_name_plural = "Уведомления мобильной консоли"
        constraints = [
            models.UniqueConstraint(
                fields=["binding", "request", "kind", "dedupe_key"],
                name="operator_notification_identity_unique",
            )
        ]
        indexes = [
            models.Index(
                fields=["binding", "status", "next_attempt_at"], name="operator_notif_due_idx"
            )
        ]

    def __str__(self):
        return f"{self.binding} · {self.request} · {self.get_kind_display()}"


class OperatorConsoleRuntime(models.Model):
    """Singleton cursor used by both bot workers to discover new requests."""

    SINGLETON_PK = 1
    singleton = models.BooleanField(default=True, unique=True, editable=False)
    announce_requests_since = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Состояние мобильной консоли"
        verbose_name_plural = "Состояние мобильной консоли"

    def __str__(self):
        return f"Консоль операторов · {self.updated_at:%Y-%m-%d %H:%M}"
