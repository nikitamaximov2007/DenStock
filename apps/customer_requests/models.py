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

from apps.core.phones import normalize_phone


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

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
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

    class Meta:
        verbose_name = "Заявка клиента"
        verbose_name_plural = "Заявки клиентов"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["status", "-created_at"], name="custreq_status_created_idx"),
        ]

    def __str__(self) -> str:
        return f"Заявка {self.public_id} ({self.customer_name})"

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
        return self.reference_for(self.public_id)


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
    # The conversation the operator's next plain text answers, if any.
    reply_conversation = models.ForeignKey(
        TelegramConversation,
        verbose_name="Отвечает на",
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
    attempts = models.PositiveSmallIntegerField("Попыток", default=0)
    next_attempt_at = models.DateTimeField("Следующая попытка", null=True, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    sent_at = models.DateTimeField("Отправлено", null=True, blank=True)

    class Meta:
        verbose_name = "Сообщение Telegram"
        verbose_name_plural = "Сообщения Telegram"
        ordering = ["created_at", "pk"]
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
