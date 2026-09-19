"""The PRO-STOR customer account domain.

A ``CustomerAccount`` is the customer, not a messenger. MAX and Telegram
identities belong TO an account (``CustomerIdentity``); an identity is never an
account by itself, and two accounts are never merged because a name, a
username or a typed phone looks alike.

Nothing here touches DenisStock employees: ``django.contrib.auth`` users are
staff, and a customer session is a separate table with its own cookie on a
separate runtime. A customer can never acquire an employee permission because
the two never share a model.

Stored on purpose, and only this: an opaque public id, a display name, provider
numeric ids with a display snapshot, timestamps and audit. No provider tokens,
no full profile JSON, no avatars.

Secrets are never stored raw: session tokens, login tokens, browser secrets and
one-time codes are kept as SHA-256 digests (``tokens.digest``).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q, Value


class CustomerAccount(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        DEACTIVATED = "deactivated", "Деактивирован"

    public_id = models.UUIDField(default=uuid.uuid4, unique=True, editable=False)
    display_name = models.CharField("Имя", max_length=120, blank=True, db_default=Value(""))
    status = models.CharField(
        "Состояние",
        max_length=16,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_default=Value(Status.ACTIVE),
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлён", auto_now=True)
    last_login_at = models.DateTimeField("Последний вход", null=True, blank=True)
    deactivated_at = models.DateTimeField("Деактивирован", null=True, blank=True)

    class Meta:
        verbose_name = "Кабинет клиента"
        verbose_name_plural = "Кабинеты клиентов"
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"Кабинет {self.code}"

    @property
    def is_active(self) -> bool:
        return self.status == self.Status.ACTIVE

    @property
    def code(self) -> str:
        """What a customer reads out to an employee to link their card.

        A display handle, never authorization: linking is an explicit employee
        action on the DenisStock side, and the code alone opens nothing.
        """
        return str(self.public_id).split("-", 1)[0].upper()


class Provider(models.TextChoices):
    MAX = "max", "MAX"
    TELEGRAM = "telegram", "Telegram"


class CustomerIdentity(models.Model):
    """One verified messenger identity owned by one account."""

    account = models.ForeignKey(
        CustomerAccount, on_delete=models.CASCADE, related_name="identities"
    )
    provider = models.CharField("Мессенджер", max_length=16, choices=Provider.choices)
    # The numeric id the provider's own server reported. Usernames, display
    # names and phone text are snapshots at best and authorize nothing.
    provider_user_id = models.BigIntegerField("ID в мессенджере")
    display_name = models.CharField("Имя (снимок)", max_length=160, blank=True)
    verified_at = models.DateTimeField("Подтверждён")
    created_at = models.DateTimeField("Добавлен", auto_now_add=True)

    class Meta:
        verbose_name = "Мессенджер кабинета"
        verbose_name_plural = "Мессенджеры кабинетов"
        constraints = [
            # The hard rule: one provider identity, one account, ever at once.
            models.UniqueConstraint(
                fields=["provider", "provider_user_id"], name="customer_identity_unique"
            ),
            # One identity per provider per account keeps "MAX ✓ / Telegram ✓"
            # meaningful and linking unambiguous.
            models.UniqueConstraint(
                fields=["account", "provider"], name="customer_identity_one_per_provider"
            ),
            models.CheckConstraint(
                condition=Q(provider_user_id__gt=0), name="customer_identity_positive_id"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_provider_display()} {self.provider_user_id}"


class CustomerLoginAttempt(models.Model):
    """A one-time, expiring, browser-bound login or link transaction.

    Flow: the browser creates the attempt and keeps ``browser_secret`` in an
    HttpOnly cookie; the customer opens the bot with ``token``; the bot, which
    receives the provider's own identity for that user, records it and sends a
    one-time code to that messenger user; the customer types the code into the
    SAME browser. The code travels provider → person → browser, never the
    other way, so a login link sent to a victim cannot log an attacker in.
    """

    class Purpose(models.TextChoices):
        LOGIN = "login", "Вход"
        LINK = "link", "Подключение мессенджера"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает мессенджер"
        CODE_SENT = "code_sent", "Код отправлен"
        COMPLETED = "completed", "Завершена"
        FAILED = "failed", "Отклонена"

    purpose = models.CharField("Назначение", max_length=8, choices=Purpose.choices)
    provider = models.CharField("Мессенджер", max_length=16, choices=Provider.choices)
    status = models.CharField(
        "Состояние", max_length=16, choices=Status.choices, default=Status.PENDING
    )
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    browser_hash = models.CharField(max_length=64, unique=True, editable=False)
    # For a link: the account that asked, taken from its live session.
    account = models.ForeignKey(
        CustomerAccount,
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="+",
    )
    provider_user_id = models.BigIntegerField(null=True, blank=True)
    provider_chat_id = models.BigIntegerField(null=True, blank=True)
    display_name = models.CharField(max_length=160, blank=True)
    code_hash = models.CharField(max_length=64, blank=True, editable=False)
    code_tries = models.PositiveSmallIntegerField(default=0)
    codes_sent = models.PositiveSmallIntegerField(default=0)
    failure = models.CharField(max_length=32, blank=True)
    client_hash = models.CharField(max_length=64, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    verified_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Попытка входа"
        verbose_name_plural = "Попытки входа"
        indexes = [
            models.Index(fields=["client_hash", "created_at"], name="customer_attempt_rate_idx"),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~Q(purpose="link") | Q(account__isnull=False),
                name="customer_attempt_link_has_account",
            ),
        ]

    def __str__(self) -> str:
        return f"Попытка {self.get_purpose_display()} #{self.pk}"



class CustomerSession(models.Model):
    """A customer's signed-in browser. The cookie holds the token; only its
    digest is stored, so a database reader cannot impersonate anyone."""

    account = models.ForeignKey(CustomerAccount, on_delete=models.CASCADE, related_name="+")
    token_hash = models.CharField(max_length=64, unique=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Сессия кабинета"
        verbose_name_plural = "Сессии кабинетов"

    def __str__(self) -> str:
        return f"Сессия кабинета #{self.pk}"



class CustomerAccountCustomerLink(models.Model):
    """An employee's explicit statement: this account is this DenisStock client.

    Never inferred from equal names or similar phone text. Purchase history is
    shown only through an ACTIVE link, and unlinking keeps the row for audit.
    """

    account = models.ForeignKey(CustomerAccount, on_delete=models.CASCADE, related_name="+")
    customer = models.ForeignKey(
        "customers.Customer", on_delete=models.PROTECT, related_name="+"
    )
    linked_at = models.DateTimeField(auto_now_add=True)
    linked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    unlinked_at = models.DateTimeField(null=True, blank=True)
    unlinked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Связь кабинета с карточкой клиента"
        verbose_name_plural = "Связи кабинетов с карточками клиентов"
        constraints = [
            models.UniqueConstraint(
                fields=["account"],
                condition=Q(unlinked_at__isnull=True),
                name="customer_link_one_active_per_account",
            ),
            models.UniqueConstraint(
                fields=["customer"],
                condition=Q(unlinked_at__isnull=True),
                name="customer_link_one_active_per_customer",
            ),
        ]

    def __str__(self) -> str:
        return f"Кабинет {self.account_id} ↔ клиент {self.customer_id}"



class CustomerConsent(models.Model):
    """Evidence that the customer gave (or withdrew) one specific consent.

    One row per purpose and version: 152-FZ art. 9 wants consent given
    separately from other documents, so each purpose is its own action.
    """

    class Purpose(models.TextChoices):
        ACCOUNT = "account", "Обработка персональных данных для личного кабинета"

    account = models.ForeignKey(CustomerAccount, on_delete=models.CASCADE, related_name="+")
    purpose = models.CharField("Цель", max_length=32, choices=Purpose.choices)
    document_version = models.CharField("Версия текста", max_length=64)
    action = models.CharField("Действие", max_length=64)
    accepted_at = models.DateTimeField("Дано", auto_now_add=True)
    withdrawn_at = models.DateTimeField("Отозвано", null=True, blank=True)

    class Meta:
        verbose_name = "Согласие клиента"
        verbose_name_plural = "Согласия клиентов"

    def __str__(self) -> str:
        return f"Согласие {self.purpose} {self.document_version}"



class CustomerAccountEvent(models.Model):
    """Audit of everything that changes who can reach an account."""

    class Kind(models.TextChoices):
        CREATED = "created", "Создан"
        LOGIN = "login", "Вход"
        LOGOUT = "logout", "Выход"
        IDENTITY_LINKED = "identity_linked", "Мессенджер подключён"
        IDENTITY_UNLINKED = "identity_unlinked", "Мессенджер отключён"
        REQUESTS_CLAIMED = "requests_claimed", "Заявки привязаны"
        CUSTOMER_LINKED = "customer_linked", "Карточка клиента привязана"
        CUSTOMER_UNLINKED = "customer_unlinked", "Карточка клиента отвязана"
        CONSENT_GIVEN = "consent_given", "Согласие дано"
        CONSENT_WITHDRAWN = "consent_withdrawn", "Согласие отозвано"
        PROFILE_CHANGED = "profile_changed", "Профиль изменён"
        DEACTIVATED = "deactivated", "Деактивирован"

    account = models.ForeignKey(CustomerAccount, on_delete=models.CASCADE, related_name="+")
    kind = models.CharField(max_length=32, choices=Kind.choices)
    detail = models.JSONField(default=dict, blank=True)
    actor_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Событие кабинета"
        verbose_name_plural = "События кабинетов"
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"{self.get_kind_display()} #{self.pk}"
