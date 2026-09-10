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
