from django.conf import settings
from django.db import models

from apps.inventory.models import NumberSequence


class InventoryCountDocument(models.Model):
    """Документ инвентаризации (Слой 20): сверка фактического наличия лотов с
    системой и корректировка остатков через ADJUST_IN/ADJUST_OUT.

    Это акт СВЕРКИ факта с системой, а НЕ списание/возврат/продажа/ремонт:
    при `counted ≠ live` документ приводит `StockLot.quantity` к факту. Физическая
    корректировка идёт ТОЛЬКО через `apps.inventory.adjust_stock_lot_quantity`: сам
    документ `StockMovement`/`StockBalance`/`StockLot.quantity` напрямую не пишет.
    Проведённый документ неизменяем; откат — встречная инвентаризация (будущий слой).

    Слой узкий: инвентаризация КОЛИЧЕСТВЕННЫХ лотов по ячейке. Поштучный `PartItem`,
    создание новых деталей/партий, сканер — вне слоя.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COMPLETED = "completed", "Проведён"
        CANCELED = "canceled", "Отменён"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    scope_location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Ячейка (область сверки)",
        on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто создал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField("Проведён (когда)", null=True, blank=True)
    canceled_at = models.DateTimeField("Отменён (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Документ инвентаризации"
        verbose_name_plural = "Документы инвентаризации"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.number

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("inventory_count")
        super().save(*args, **kwargs)


class InventoryCountLine(models.Model):
    """Строка инвентаризации: один лот. `expected_quantity` — снимок системного
    количества на момент добавления (для UI/истории); фактическая дельта при
    проведении считается от ЖИВОГО `StockLot.quantity` (source of truth, §7/§9).
    `adjustment` — созданное при проведении движение (если `counted ≠ live`).
    """

    count_document = models.ForeignKey(
        InventoryCountDocument, verbose_name="Документ",
        on_delete=models.CASCADE, related_name="lines",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот",
        on_delete=models.PROTECT, related_name="count_lines",
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="+",
    )
    location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Ячейка",
        on_delete=models.PROTECT, related_name="+",
    )
    expected_quantity = models.DecimalField(
        "Системное кол-во (снимок)", max_digits=12, decimal_places=3
    )
    counted_quantity = models.DecimalField(
        "Фактическое кол-во", max_digits=12, decimal_places=3, null=True, blank=True
    )
    unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    adjustment = models.ForeignKey(
        "inventory.StockMovement", verbose_name="Движение корректировки",
        on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция инвентаризации"
        verbose_name_plural = "Позиции инвентаризации"
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(
                fields=["count_document", "stock_lot"], name="uniq_countline_doc_lot"
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(counted_quantity__isnull=True)
                    | models.Q(counted_quantity__gte=0)
                ),
                name="countline_counted_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.part_type} @ {self.location.code} (лот #{self.stock_lot_id})"

    @property
    def difference(self):
        """counted − expected (None, если ещё не сосчитано) — дисплейная величина."""
        if self.counted_quantity is None:
            return None
        return self.counted_quantity - self.expected_quantity
