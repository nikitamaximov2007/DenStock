from django.conf import settings
from django.db import models

from apps.inventory.models import NumberSequence


class StockReturn(models.Model):
    """Документ возврата на склад (Слой 18): физическое обратное поступление
    проданной (Слой 16) или выданной в ремонт (Слой 17) детали.

    Это НЕ денежный refund/чек/сторно: документ возвращает физический остаток и
    порождает приходное движение, но финансовую историю `Sale`/`RepairOrder` не
    меняет и их статус `completed` не трогает. Физическое поступление идёт ТОЛЬКО
    через сервисы `apps.inventory` (`return_part_item`/`return_stock_lot_quantity`):
    сам возврат ledger (`StockMovement`/`StockBalance`/статусы/quantity) не пишет.
    Проведённый возврат неизменяем; отмена проведённого — будущий слой корректировок.

    Возврат оформляется из ОДНОГО документа-источника: `source_type` ∈
    {sale, repair_order}, `source_id` — id `Sale`/`RepairOrder` (лёгкий указатель,
    как `StockMovement.document_*`, без contenttypes).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COMPLETED = "completed", "Проведён"

    class SourceType(models.TextChoices):
        SALE = "sale", "Продажа"
        REPAIR_ORDER = "repair_order", "Ремонтный заказ"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    source_type = models.CharField("Тип источника", max_length=20, choices=SourceType.choices)
    source_id = models.PositiveIntegerField("ID источника")
    reason = models.CharField("Причина возврата", max_length=255, blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    cost_total = models.DecimalField(
        "Себестоимость возвращённого (₽)", max_digits=14, decimal_places=2, default=0
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто создал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField("Проведён (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Возврат на склад"
        verbose_name_plural = "Возвраты на склад"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.get_source_type_display()} #{self.source_id})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("stock_return")
        super().save(*args, **kwargs)


class StockReturnLine(models.Model):
    """Строка возврата: одна исходная строка `SaleLine` XOR `RepairIssueLine`.

    Денормализует объект (экземпляр/лот) из источника, хранит ячейку возврата и
    целевое состояние (карантин/доступен). Себестоимость (`unit_cost_rub`/
    `total_cost_rub`) замораживается из исходной строки в момент проведения и не
    пересчитывается от текущего landed cost. `returned_lot` — лот, в который
    фактически зачислено количество (для лотов; заполняется при проведении).
    """

    class RestockStatus(models.TextChoices):
        AVAILABLE = "available", "Доступен"
        QUARANTINE = "quarantine", "Карантин"

    stock_return = models.ForeignKey(
        StockReturn, verbose_name="Возврат", on_delete=models.CASCADE, related_name="lines"
    )
    source_sale_line = models.ForeignKey(
        "sales.SaleLine", verbose_name="Строка продажи",
        on_delete=models.PROTECT, null=True, blank=True, related_name="return_lines",
    )
    source_repair_line = models.ForeignKey(
        "repairs.RepairIssueLine", verbose_name="Строка выдачи в ремонт",
        on_delete=models.PROTECT, null=True, blank=True, related_name="return_lines",
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    part_item = models.ForeignKey(
        "inventory.PartItem", verbose_name="Экземпляр",
        on_delete=models.PROTECT, null=True, blank=True, related_name="return_lines",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот-источник",
        on_delete=models.PROTECT, null=True, blank=True, related_name="return_source_lines",
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия", on_delete=models.PROTECT, related_name="+"
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="+",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    to_location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Ячейка возврата",
        on_delete=models.PROTECT, related_name="+",
    )
    restock_status = models.CharField(
        "Состояние возврата", max_length=20, choices=RestockStatus.choices,
        default=RestockStatus.QUARANTINE,
    )
    unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    total_cost_rub = models.DecimalField(
        "Себестоимость строки (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    returned_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот зачисления",
        on_delete=models.PROTECT, null=True, blank=True, related_name="return_target_lines",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция возврата"
        verbose_name_plural = "Позиции возврата"
        ordering = ["id"]
        constraints = [
            # Источник — ровно один: строка продажи ИЛИ строка выдачи в ремонт.
            models.CheckConstraint(
                condition=(
                    models.Q(source_sale_line__isnull=False, source_repair_line__isnull=True)
                    | models.Q(source_sale_line__isnull=True, source_repair_line__isnull=False)
                ),
                name="returnline_source_xor",
            ),
            # Объект — ровно один: экземпляр ИЛИ лот.
            models.CheckConstraint(
                condition=(
                    models.Q(part_item__isnull=False, stock_lot__isnull=True)
                    | models.Q(part_item__isnull=True, stock_lot__isnull=False)
                ),
                name="returnline_item_xor_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="returnline_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        target = self.part_item or self.stock_lot
        return f"{self.part_type} × {self.quantity} → {self.to_location} ({target})"
