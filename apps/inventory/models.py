from django.core.exceptions import ValidationError
from django.db import models, transaction


class NumberSequence(models.Model):
    """Атомарный счётчик внутренних номеров.

    Одна строка на ключ; выдача номера идёт под блокировкой этой строки
    (`select_for_update`). В отличие от блокировки последней записи целевой
    таблицы, строка-счётчик существует всегда — поэтому выдача сериализуется
    даже при пустой таблице (нет гонки на первом номере). Строки заводятся
    data-миграцией.
    """

    key = models.CharField("Ключ", max_length=50, unique=True)
    prefix = models.CharField("Префикс", max_length=10)
    last_value = models.PositiveIntegerField("Последнее значение", default=0)

    class Meta:
        verbose_name = "Счётчик номеров"
        verbose_name_plural = "Счётчики номеров"

    def __str__(self) -> str:
        return f"{self.key}: {self.prefix}{self.last_value:06d}"

    @classmethod
    def next(cls, key: str) -> str:
        """Выдать следующий номер вида PREFIX000001 под блокировкой строки."""
        with transaction.atomic():
            seq = cls.objects.select_for_update().get(key=key)
            seq.last_value += 1
            seq.save(update_fields=["last_value"])
            return f"{seq.prefix}{seq.last_value:06d}"


class PartItem(models.Model):
    """Физический поштучный экземпляр детали из финансово закрытой партии.

    Это ещё не складское движение: `StockMovement`/`StockBalance` появятся на
    Слое 10. Экземпляр создаётся вручную из строки партии (Слой 8), без сканера.
    """

    class Status(models.TextChoices):
        RECEIVING = "receiving", "На приёмке"
        AVAILABLE = "available", "Доступен"
        RESERVED = "reserved", "Зарезервирован"
        SOLD = "sold", "Продан"
        INSTALLED = "installed", "Установлен"
        REPAIR = "repair", "В ремонте"
        WRITTEN_OFF = "written_off", "Списан"
        RETURNED = "returned", "Возвращён"
        QUARANTINE = "quarantine", "Карантин"

    # Переходы, разрешённые вручную на Слое 8. Остальные статусы
    # (reserved/sold/installed/...) выставляются своими слоями через движения.
    ALLOWED_TRANSITIONS = {
        Status.RECEIVING: [Status.AVAILABLE, Status.QUARANTINE],
        Status.AVAILABLE: [Status.QUARANTINE],
        Status.QUARANTINE: [Status.AVAILABLE],
    }

    internal_number = models.CharField(
        "Внутренний номер", max_length=20, unique=True, editable=False
    )
    internal_barcode = models.CharField(
        "Внутренний штрихкод", max_length=40, unique=True, editable=False
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="items"
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия", on_delete=models.PROTECT, related_name="items"
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="items",
    )
    serial_number = models.CharField("Серийный номер", max_length=100, blank=True)
    landed_cost_rub = models.DecimalField(
        "Себестоимость (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.RECEIVING
    )
    current_location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Место",
        on_delete=models.PROTECT, null=True, blank=True, related_name="items",
    )
    note = models.CharField("Примечание", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Экземпляр детали"
        verbose_name_plural = "Экземпляры деталей"
        ordering = ["-created_at"]
        constraints = [
            # Серийник уникален в пределах вида детали, если заполнен.
            models.UniqueConstraint(
                fields=["part_type", "serial_number"],
                condition=~models.Q(serial_number=""),
                name="uniq_partitem_serial_per_parttype",
            ),
        ]

    def __str__(self) -> str:
        return self.internal_number

    def save(self, *args, **kwargs):
        if not self.internal_barcode and self.internal_number:
            self.internal_barcode = f"ITEM:{self.internal_number}"
        super().save(*args, **kwargs)

    def clean(self) -> None:
        if self.current_location_id and not self.current_location.can_hold_stock():
            raise ValidationError(
                {"current_location": "Это место не предназначено для хранения остатка."}
            )

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, [])
