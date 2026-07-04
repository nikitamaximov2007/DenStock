"""Layer 28 — «Поступление»: документ прихода поставки (несколько позиций).

Это UX-обёртка над существующей складской логикой, НЕ вторая система склада:
черновик — просто данные документа (ничего не создаёт на складе); при
проведении создаются партия/строки, фиксируется себестоимость и остаток
появляется через существующие сервисы inventory (create/receive). Проведённый
документ read-only; отмена проведённого не реализуется (Layer 28).
"""
from django.conf import settings
from django.db import models

from apps.inventory.models import NumberSequence


class Receipt(models.Model):
    """Шапка поступления: от кого, когда, кто создал/провёл."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        POSTED = "posted", "Проведено"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    supplier = models.ForeignKey(
        "suppliers.Supplier", verbose_name="Поставщик",
        on_delete=models.PROTECT, null=True, blank=True, related_name="receipts",
    )
    received_at = models.DateField("Дата поступления", null=True, blank=True)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    # Партия, созданная при проведении (для просмотра связей).
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия",
        on_delete=models.PROTECT, null=True, blank=True,
        related_name="receipts", editable=False,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто создал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    posted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто провёл",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    posted_at = models.DateTimeField("Проведено (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Поступление"
        verbose_name_plural = "Поступления"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.supplier or 'без поставщика'})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("receipt")
        super().save(*args, **kwargs)

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT


class ReceiptLine(models.Model):
    """Позиция поступления: деталь × количество × себестоимость × ячейка."""

    receipt = models.ForeignKey(Receipt, on_delete=models.CASCADE, related_name="lines")
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_cost_rub = models.DecimalField(
        "Цена за ед. (₽)", max_digits=12, decimal_places=2, default=0
    )
    location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Ячейка",
        on_delete=models.PROTECT, related_name="+",
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    # Строка партии, созданная при проведении (для просмотра связей).
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция поступления"
        verbose_name_plural = "Позиции поступления"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="receiptline_quantity_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(unit_cost_rub__gte=0),
                name="receiptline_cost_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.part_type} × {self.quantity}"

    @property
    def total_cost_rub(self):
        return self.quantity * self.unit_cost_rub
