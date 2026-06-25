from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction

from apps.procurement.models import money


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


class StockMovement(models.Model):
    """Неизменяемый журнал (append-only) любого изменения физического остатка.

    Источник истины об инварианте: ни одна дельта `StockLot.quantity` или
    `PartItem.status/location` не проходит мимо сервиса движения. Записи нельзя
    редактировать или удалять — только добавлять (Слой 10). Коммерческие типы
    (продажа/списание/…) навесятся на эту же таблицу в Слоях 15–19.
    """

    class MovementType(models.TextChoices):
        RECEIVE_ITEM = "receive_item", "Приёмка экземпляра"
        RECEIVE_LOT = "receive_lot", "Приёмка лота"
        MOVE_ITEM = "move_item", "Перемещение экземпляра"
        MOVE_LOT = "move_lot", "Перемещение лота"
        ADJUST_IN = "adjust_in", "Корректировка +"
        ADJUST_OUT = "adjust_out", "Корректировка −"
        SALE_ITEM = "sale_item", "Продажа экземпляра"
        SALE_LOT = "sale_lot", "Продажа лота"

    movement_type = models.CharField(
        "Тип движения", max_length=20, choices=MovementType.choices
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь",
        on_delete=models.PROTECT, related_name="movements",
    )
    # Ровно один из part_item / stock_lot задан (XOR, см. CheckConstraint).
    part_item = models.ForeignKey(
        "inventory.PartItem", verbose_name="Экземпляр",
        on_delete=models.PROTECT, null=True, blank=True, related_name="movements",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот",
        on_delete=models.PROTECT, null=True, blank=True, related_name="movements",
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия",
        on_delete=models.PROTECT, null=True, blank=True, related_name="movements",
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, null=True, blank=True, related_name="movements",
    )
    from_location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Откуда",
        on_delete=models.PROTECT, null=True, blank=True, related_name="movements_out",
    )
    to_location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Куда",
        on_delete=models.PROTECT, null=True, blank=True, related_name="movements_in",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    total_cost_rub = models.DecimalField(
        "Себестоимость движения (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто провёл",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    # Лёгкий указатель на документ-источник (без contenttypes): пусто на Слое 10,
    # навесится при появлении Sale/WriteOff (Слои 16–19).
    document_type = models.CharField("Тип документа", max_length=40, blank=True)
    document_id = models.PositiveIntegerField("ID документа", null=True, blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)

    class Meta:
        verbose_name = "Складское движение"
        verbose_name_plural = "Складские движения"
        ordering = ["-created_at"]
        constraints = [
            # Движение относится либо к экземпляру, либо к лоту — ровно к одному.
            models.CheckConstraint(
                condition=(
                    models.Q(part_item__isnull=False, stock_lot__isnull=True)
                    | models.Q(part_item__isnull=True, stock_lot__isnull=False)
                ),
                name="stockmovement_item_xor_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="stockmovement_quantity_positive"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.get_movement_type_display()} {self.part_type} × {self.quantity}"

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise RuntimeError(
                "StockMovement неизменяем (append-only): запись нельзя редактировать."
            )
        self.total_cost_rub = money(self.unit_cost_rub * self.quantity)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("StockMovement неизменяем (append-only): запись нельзя удалить.")


class StockBalance(models.Model):
    """Read-optimized кэш текущих остатков. НЕ источник истины.

    Полностью пересобирается из первички (`StockLot` + `PartItem`) командой
    `rebuild_stock_balance`; правка руками (форм/админки) запрещена. Грань —
    `(batch_line, location)`: для bulk совпадает с лотом 1:1, для serial
    агрегирует экземпляры. На Слое 10 `reserved`/`in_repair` всегда 0.
    """

    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь",
        on_delete=models.PROTECT, related_name="balances",
    )
    location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Место",
        on_delete=models.PROTECT, related_name="balances",
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия",
        on_delete=models.PROTECT, related_name="balances",
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="balances",
    )
    quantity_physical = models.DecimalField(
        "Физически в ячейке", max_digits=12, decimal_places=3, default=0
    )
    quantity_available = models.DecimalField(
        "Доступно", max_digits=12, decimal_places=3, default=0
    )
    quantity_reserved = models.DecimalField(
        "Зарезервировано", max_digits=12, decimal_places=3, default=0
    )
    quantity_in_repair = models.DecimalField(
        "В ремонте", max_digits=12, decimal_places=3, default=0
    )
    quantity_quarantine = models.DecimalField(
        "В карантине", max_digits=12, decimal_places=3, default=0
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Остаток (кэш)"
        verbose_name_plural = "Остатки (кэш)"
        ordering = ["part_type_id", "location_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["batch_line", "location"], name="uniq_stockbalance_line_location"
            ),
        ]
        indexes = [
            models.Index(fields=["part_type", "location"], name="stockbalance_part_loc_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.part_type} @ {self.location.code}: {self.quantity_physical}"


class StockLot(models.Model):
    """Количественный складской лот: количество bulk-детали в конкретной ячейке.

    Создаётся из финансово закрытой строки партии (Слой 9). Это ещё не ledger:
    `StockMovement`/`StockBalance` появятся на Слое 10. `quantity` — текущее
    количество лота; `initial_quantity` фиксируется при создании и далее не
    меняется (станет осмысленным с приходом движений).
    """

    class Status(models.TextChoices):
        RECEIVING = "receiving", "На приёмке"
        AVAILABLE = "available", "Доступен"
        QUARANTINE = "quarantine", "Карантин"
        WRITTEN_OFF = "written_off", "Списан"
        # Лот обнулён движением (корректировка/будущий расход). Ставится
        # автоматически сервисом при quantity == 0 (Слой 10) и не считается в
        # физическом остатке.
        DEPLETED = "depleted", "Исчерпан"

    # Ручные переходы Слоя 9. written_off выставляется Слоем 19 через движения.
    ALLOWED_TRANSITIONS = {
        Status.RECEIVING: [Status.AVAILABLE, Status.QUARANTINE],
        Status.AVAILABLE: [Status.QUARANTINE],
        Status.QUARANTINE: [Status.AVAILABLE],
    }

    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь",
        on_delete=models.PROTECT, related_name="stock_lots",
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия",
        on_delete=models.PROTECT, related_name="stock_lots",
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="lots",
    )
    location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Место",
        on_delete=models.PROTECT, related_name="stock_lots",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    initial_quantity = models.DecimalField(
        "Исходное количество", max_digits=12, decimal_places=3, editable=False
    )
    landed_unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.RECEIVING
    )
    note = models.CharField("Примечание", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Складской лот"
        verbose_name_plural = "Складские лоты"
        ordering = ["-created_at"]
        constraints = [
            # Один лот на пару «строка партии × ячейка»; разные ячейки — разные лоты.
            models.UniqueConstraint(
                fields=["batch_line", "location"], name="uniq_stocklot_line_location"
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gte=0), name="stocklot_quantity_non_negative"
            ),
            models.CheckConstraint(
                condition=models.Q(initial_quantity__gte=0),
                name="stocklot_initial_non_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.part_type} × {self.quantity} @ {self.location.code}"

    def clean(self) -> None:
        if self.location_id and not self.location.can_hold_stock():
            raise ValidationError(
                {"location": "Это место не предназначено для хранения остатка."}
            )

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, [])
