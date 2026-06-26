from django.conf import settings
from django.db import models

from apps.inventory.models import NumberSequence


class WriteOffDocument(models.Model):
    """Документ списания (Слой 19): документированное складское выбытие детали по
    причине (брак/потеря/повреждение/утилизация/неликвид/прочее).

    Это НЕ продажа/ремонт/возврат/инвентаризация и НЕ финансовый документ оплаты:
    документ уменьшает физический остаток, фиксирует причину и замораживает
    себестоимость потерь, но денежной стороны не имеет. Физическое выбытие идёт
    ТОЛЬКО через сервисы `apps.inventory` (`write_off_part_item`/
    `write_off_stock_lot_quantity`): сам документ ledger (`StockMovement`/
    `StockBalance`/статусы/quantity) не пишет. Проведённое списание неизменяемо;
    отмена проведённого (восстановление остатка) — будущий слой корректировок.

    Причина — на документе (одно списание = одна причина); один документ может
    списывать несколько экземпляров/лотов по этой причине.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COMPLETED = "completed", "Проведён"
        CANCELED = "canceled", "Отменён"

    class Reason(models.TextChoices):
        DAMAGED = "damaged", "Повреждение"
        LOST = "lost", "Потеря"
        DEFECT = "defect", "Брак"
        DISPOSAL = "disposal", "Утилизация"
        OBSOLETE = "obsolete", "Неликвид"
        OTHER = "other", "Прочее"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    reason = models.CharField("Причина", max_length=20, choices=Reason.choices)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    cost_total = models.DecimalField(
        "Себестоимость списанного (₽)", max_digits=14, decimal_places=2, default=0
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто создал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField("Проведён (когда)", null=True, blank=True)
    canceled_at = models.DateTimeField("Отменён (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Документ списания"
        verbose_name_plural = "Документы списания"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.get_reason_display()})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("write_off")
        super().save(*args, **kwargs)


class WriteOffLine(models.Model):
    """Строка списания: экземпляр целиком ИЛИ количество из лота (XOR).

    Себестоимость (`unit_cost_rub`/`total_cost_rub`) замораживается при проведении
    и не пересчитывается от будущих изменений landed cost. Цены/прибыли нет — это
    потери по себестоимости, а не продажа.
    """

    write_off = models.ForeignKey(
        WriteOffDocument, verbose_name="Документ списания",
        on_delete=models.CASCADE, related_name="lines",
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    part_item = models.ForeignKey(
        "inventory.PartItem", verbose_name="Экземпляр",
        on_delete=models.PROTECT, null=True, blank=True, related_name="write_off_lines",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот",
        on_delete=models.PROTECT, null=True, blank=True, related_name="write_off_lines",
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия", on_delete=models.PROTECT, related_name="+"
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="+",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    total_cost_rub = models.DecimalField(
        "Себестоимость строки (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    note = models.CharField("Примечание", max_length=255, blank=True)
    written_off_at = models.DateTimeField("Списано (когда)", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция списания"
        verbose_name_plural = "Позиции списания"
        ordering = ["id"]
        constraints = [
            # Позиция относится либо к экземпляру, либо к лоту — ровно к одному.
            models.CheckConstraint(
                condition=(
                    models.Q(part_item__isnull=False, stock_lot__isnull=True)
                    | models.Q(part_item__isnull=True, stock_lot__isnull=False)
                ),
                name="writeoffline_item_xor_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="writeoffline_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        target = self.part_item or self.stock_lot
        return f"{self.part_type} × {self.quantity} ({target})"
