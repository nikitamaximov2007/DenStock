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
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Клиент"
        verbose_name_plural = "Клиенты"
        ordering = ["name", "pk"]
        indexes = [models.Index(fields=["name"], name="customer_name_idx")]

    def __str__(self) -> str:
        return self.name

    def save(self, *args, **kwargs):
        self.name = (self.name or "").strip()
        self.phone = (self.phone or "").strip()
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
        return f"{self.customer}: {self.period_start}–{self.period_end}"
