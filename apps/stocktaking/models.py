from django.conf import settings
from django.db import models
from django.db.models import Q

from apps.inventory.models import NumberSequence, StockLot


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


class SectionRecount(models.Model):
    """Атомарная пересборка факта по фиксированному участку склада.

    Документ отделён от первичного scanner-ввода: строки здесь только фиксируют
    факт, а остатки меняются позднее, одной транзакцией через inventory service.
    ``snapshot`` и ``result`` являются неизменяемым аудитом входа/результата.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COUNTING = "counting", "Идёт пересчёт"
        READY = "ready", "Готов к применению"
        APPLYING = "applying", "Применяется"
        COMPLETED = "completed", "Завершён"
        CANCELED = "canceled", "Отменён"
        FAILED = "failed", "Ошибка"

    section_code = models.CharField("Участок", max_length=60, db_index=True)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT, db_index=True
    )
    operation_key = models.CharField("Ключ операции", max_length=64, unique=True, editable=False)
    snapshot = models.JSONField("Исходный snapshot", default=dict, editable=False)
    snapshot_fingerprint = models.CharField(
        "Fingerprint snapshot", max_length=64, blank=True, editable=False
    )
    result = models.JSONField("Результат", default=dict, editable=False)
    error_message = models.CharField("Ошибка", max_length=500, blank=True, editable=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто создал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="section_recounts",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField("Начат", null=True, blank=True)
    completed_at = models.DateTimeField("Завершён", null=True, blank=True)
    canceled_at = models.DateTimeField("Отменён", null=True, blank=True)
    failed_at = models.DateTimeField("Ошибка (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Пересчёт участка"
        verbose_name_plural = "Пересчёты участков"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["section_code"],
                condition=Q(status__in=["draft", "counting", "ready", "applying"]),
                name="uniq_active_section_recount",
            ),
        ]

    def __str__(self):
        return f"Пересчёт участка {self.section_code} #{self.pk}"

    @property
    def is_mutable(self):
        return self.status in {
            self.Status.DRAFT, self.Status.COUNTING, self.Status.READY
        }


class SectionRecountCell(models.Model):
    """Одна физическая ячейка и её подтверждённый этап пересчёта."""

    class Status(models.TextChoices):
        NOT_STARTED = "not_started", "Не начато"
        COUNTING = "counting", "В работе"
        COMPLETED = "completed", "Пересчитана"

    recount = models.ForeignKey(SectionRecount, on_delete=models.CASCADE, related_name="cells")
    location = models.ForeignKey(
        "warehouse.StorageLocation", on_delete=models.PROTECT, related_name="section_recount_cells"
    )
    sequence = models.PositiveSmallIntegerField("Порядок")
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.NOT_STARTED
    )
    counted_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["sequence", "id"]
        constraints = [
            models.UniqueConstraint(fields=["recount", "location"], name="uniq_recount_cell"),
            models.UniqueConstraint(fields=["recount", "sequence"], name="uniq_recount_sequence"),
        ]

    def __str__(self):
        return f"{self.recount.section_code}: {self.location.code}"


class SectionRecountLine(models.Model):
    """Фактически посчитанная деталь в конкретной ячейке."""

    recount = models.ForeignKey(SectionRecount, on_delete=models.CASCADE, related_name="lines")
    cell = models.ForeignKey(SectionRecountCell, on_delete=models.CASCADE, related_name="lines")
    part_type = models.ForeignKey(
        "catalog.PartType", on_delete=models.PROTECT, related_name="section_recount_lines"
    )
    part_number = models.CharField("Артикул (снимок)", max_length=100)
    quantity = models.DecimalField("Факт", max_digits=12, decimal_places=3, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["cell__sequence", "part_number", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["cell", "part_type"], name="uniq_section_recount_part_cell"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gte=0), name="section_recount_qty_nonnegative"
            ),
        ]

    def __str__(self):
        return f"{self.part_number} x {self.quantity} @ {self.cell.location.code}"


class SectionRecountAllocation(models.Model):
    """Явное распределение строки факта по партии и её себестоимости."""

    line = models.ForeignKey(
        SectionRecountLine, on_delete=models.CASCADE, related_name="allocations"
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine",
        on_delete=models.PROTECT,
        related_name="section_recount_allocations",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_cost_rub = models.DecimalField(
        "Себестоимость (snapshot)", max_digits=12, decimal_places=2, editable=False
    )
    lot_status = models.CharField(
        "Статус лота (snapshot)", max_length=20, choices=StockLot.Status.choices,
        default=StockLot.Status.AVAILABLE, editable=False,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["line", "batch_line"], name="uniq_section_recount_allocation"
            ),
            models.CheckConstraint(
                condition=Q(quantity__gt=0), name="section_recount_alloc_positive"
            ),
        ]

    def __str__(self):
        return f"{self.line.part_number}: {self.quantity} из партии {self.batch_line_id}"
