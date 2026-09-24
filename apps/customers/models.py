from django.db import models

from apps.core.phones import normalize_phone


class Customer(models.Model):
    """Постоянная карточка клиента.

    До этой модели клиент существовал только строкой внутри документа, поэтому
    «тот же клиент» нельзя было выразить: два документа с одинаковым текстом
    могли принадлежать разным людям, а один человек мог быть записан по-разному.
    Карточка даёт клиенту стабильный идентификатор (PK), и именно он связывает
    документы между собой.

    Чего здесь СОЗНАТЕЛЬНО нет:

    * уникальности имени: тёзки это норма, а не ошибка ввода;
    * уникальности телефона: один номер бывает семейным, рабочим или общим для
      организации, поэтому он не идентифицирует человека;
    * автоматического слияния карточек: решение «это один человек» принимает
      сотрудник, а не эвристика.

    Документы продолжают хранить СНИМОК имени и телефона на момент проведения.
    Переименование карточки завтра не переписывает историю: см. документы
    `Sale`, `RepairOrder`, `Reservation`.
    """

    name = models.CharField("Имя клиента", max_length=255)
    phone = models.CharField("Телефон", max_length=50, blank=True)
    # Служебная форма только для поиска: цифры, российская 8 приведена к 7.
    # Индекс без уникальности: один номер законно встречается у разных карточек.
    phone_normalized = models.CharField(
        "Телефон для поиска", max_length=50, blank=True, db_index=True, editable=False
    )
    comment = models.TextField("Комментарий", blank=True)
    city = models.CharField("Город", max_length=150, blank=True)
    equipment = models.CharField("Техника", max_length=255, blank=True)
    vin = models.CharField("VIN", max_length=100, blank=True)
    mileage_at_arrival = models.PositiveIntegerField("Пробег", null=True, blank=True)
    # Слияние карточек (см. apps.customers.merge): карточка-источник никогда не
    # удаляется - на неё могут ссылаться старые открытые вкладки, закладки и
    # печатные документы, а PROTECT ниже по цепочке всё равно не даст её
    # удалить, пока не переназначены её продажи/ремонты/заявки. Вместо этого
    # источник помечается редиректом на канонический дубль: списки выбора
    # клиента (`search_customers`, `customers_by_recent_activity`) исключают
    # такие карточки, чтобы для НОВОГО документа их нельзя было выбрать
    # случайно, а прямая ссылка на карточку по-прежнему открывает её историю.
    merged_into = models.ForeignKey(
        "self",
        verbose_name="Объединена с карточкой",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="merged_from",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ["name", "pk"]
        indexes = [models.Index(fields=["name"], name="customer_name_idx")]
        constraints = [
            # Карточка не может быть объединена сама с собой, а цепочки
            # источник->источник (A слит в B, B слит в A) вообще не должны
            # возникать - merge.py проверяет это до записи, констрейнт защищает
            # от прямого обхода сервиса.
            models.CheckConstraint(
                condition=~models.Q(merged_into=models.F("id")),
                name="customer_merged_into_not_self",
            ),
        ]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.phone = (self.phone or "").strip()
        self.city = (self.city or "").strip()
        self.equipment = (self.equipment or "").strip()
        self.vin = (self.vin or "").strip()
        self.phone_normalized = normalize_phone(self.phone)
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            fields = set(update_fields)
            if "phone" in fields:
                fields.add("phone_normalized")
                kwargs["update_fields"] = sorted(fields)
        super().save(*args, **kwargs)

    def snapshot(self) -> dict:
        """Значения, которые документ замораживает у себя на момент проведения."""
        return {"customer_name": self.name, "customer_phone": self.phone}

    @property
    def is_merged(self) -> bool:
        return self.merged_into_id is not None


class CustomerCreateIdempotency(models.Model):
    """Durable receipt for one rendered customer-create operation."""

    token = models.UUIDField("Ключ создания", unique=True, editable=False)
    customer = models.OneToOneField(
        Customer,
        verbose_name="Созданный клиент",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="create_idempotency_receipt",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Квитанция идемпотентности создания клиента"
        verbose_name_plural = "Квитанции идемпотентности создания клиентов"

    def __str__(self) -> str:
        return f"{self.token} → {self.customer_id or 'pending'}"


class CustomerPeriodPaymentAcknowledgement(models.Model):
    """Аудит ручного подтверждения полной оплаты клиентом за период.

    Это не кассовый документ и не меняет продажи или ремонты. Актуальность
    записи определяется сохранённым fingerprint текущих клиентских сумм в
    отчёте: при изменении состава или суммы строка логически становится
    неактуальной, а её исходные значения остаются в журнале.
    """

    customer = models.ForeignKey(
        Customer,
        verbose_name="Клиент",
        on_delete=models.PROTECT,
        related_name="payment_acknowledgements",
    )
    period_start = models.DateField("Период с")
    period_end = models.DateField("Период по")
    amount_rub = models.DecimalField("Подтверждённая сумма (₽)", max_digits=14, decimal_places=2)
    billable_fingerprint = models.CharField("Снимок состава начислений", max_length=64)
    document_count = models.PositiveIntegerField("Количество документов")
    acknowledged_at = models.DateTimeField("Подтверждено когда", auto_now_add=True)
    acknowledged_by = models.ForeignKey(
        "accounts.User",
        verbose_name="Подтвердил",
        on_delete=models.SET_NULL,
        null=True,
        related_name="+",
    )
    revoked_at = models.DateTimeField("Снято когда", null=True, blank=True)
    revoked_by = models.ForeignKey(
        "accounts.User",
        verbose_name="Снял",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Подтверждение оплаты клиента за период"
        verbose_name_plural = "Подтверждения оплаты клиентов за период"
        ordering = ["-acknowledged_at", "-pk"]
        indexes = [
            models.Index(
                fields=["customer", "period_start", "period_end", "revoked_at"],
                name="customer_period_payment_idx",
            )
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(period_end__gte=models.F("period_start")),
                name="customer_payment_period_ordered",
            )
        ]

    def __str__(self) -> str:
        return f"{self.customer}: {self.period_start}-{self.period_end}"


class CustomerMergeReceipt(models.Model):
    """Durable evidence that one Customer card was merged into another.

    Additive audit record only - it never gets edited after creation. The
    source card itself is never deleted (see ``Customer.merged_into``), so
    this receipt is a second, explicit trail rather than the only one: both
    together answer "was this ever merged, by whom, when, and how much moved."
    """

    target = models.ForeignKey(
        Customer, verbose_name="Куда объединили",
        on_delete=models.PROTECT, related_name="merge_receipts_as_target",
    )
    source = models.ForeignKey(
        Customer, verbose_name="Источник",
        on_delete=models.PROTECT, related_name="merge_receipts_as_source",
    )
    # Снимок на момент слияния: у исходной и целевой карточки телефон мог
    # позже измениться, а квитанция должна честно показывать, что их
    # объединило тогда.
    normalized_phone = models.CharField("Канонический телефон (снимок)", max_length=50, blank=True)
    performed_by = models.ForeignKey(
        "accounts.User", verbose_name="Кто выполнил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField("Когда", auto_now_add=True)
    moved_counts = models.JSONField("Перенесённые связи", default=dict)
    reason = models.CharField("Причина / комментарий", max_length=500, blank=True)

    class Meta:
        verbose_name = "Квитанция объединения клиентов"
        verbose_name_plural = "Квитанции объединения клиентов"
        ordering = ["-created_at", "-pk"]

    def __str__(self) -> str:
        return f"#{self.source_id} → #{self.target_id} ({self.created_at:%Y-%m-%d})"
