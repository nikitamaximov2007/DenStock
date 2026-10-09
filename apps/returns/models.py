from contextlib import contextmanager
from contextvars import ContextVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import connection, models, transaction

from apps.inventory.models import NumberSequence


class PostedStockReturnError(ValidationError):
    """A completed or canceled StockReturn is posted history and cannot be edited.

    Raised by the model/queryset guards.  The PostgreSQL triggers from migration
    ``0005_posted_stockreturn_guard`` enforce the same rule below the ORM.
    """


DRAFT = "draft"
COST_FIELDS_HEADER = frozenset({"cost_total"})
COST_FIELDS_LINE = frozenset({"unit_cost_rub", "total_cost_rub"})
# Columns a posted header may still change (Django manages them / SET_NULL users).
_ALWAYS_MUTABLE_HEADER = frozenset({"updated_at"})
_CANCEL_FIELDS = frozenset({"status", "canceled_at", "canceled_by", "cancel_reason"})

# Explicit, scoped permissions held only by the domain services.
_status_transition: ContextVar[tuple | None] = ContextVar("stockreturn_transition", default=None)
_cost_correction: ContextVar[bool] = ContextVar("stockreturn_cost_correction", default=False)


@contextmanager
def status_transition(pk: int, target: str):
    """Allow exactly one service-driven status change of one document."""
    token = _status_transition.set((pk, target))
    try:
        yield
    finally:
        _status_transition.reset(token)


@contextmanager
def posted_return_cost_correction():
    """Allow ONLY cost columns of posted returns to change (receipt-proven remediation).

    On PostgreSQL the same permission is passed to the trigger with a
    transaction-local setting, so it ends with the surrounding transaction.
    """
    if not transaction.get_connection().in_atomic_block:
        raise PostedStockReturnError("Cost correction must run inside a transaction.")
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('denstock.returns_cost_correction', 'on', true)")
    token = _cost_correction.set(True)
    try:
        yield
    finally:
        _cost_correction.reset(token)
        # After an error the transaction is rolled back and the local setting dies
        # with it; only a healthy transaction needs the explicit switch-off.
        if connection.vendor == "postgresql" and not connection.needs_rollback:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT set_config('denstock.returns_cost_correction', 'off', true)"
                )


def _locked_status(model, pk):
    """Current DB status, row-locked on PostgreSQL (never trust the instance)."""
    row = model._base_manager.select_for_update().filter(pk=pk).values_list(
        "status", flat=True,
    ).first()
    return row


class StockReturnQuerySet(models.QuerySet):
    """Bulk writes may not touch posted returns, and never change status."""

    def _posted(self):
        return self.exclude(status=DRAFT).exists()

    def update(self, **kwargs):
        if "status" in kwargs:
            raise PostedStockReturnError("Статус возврата меняется только сервисом.")
        if self._posted() and not (
            _cost_correction.get() and set(kwargs) <= COST_FIELDS_HEADER | _ALWAYS_MUTABLE_HEADER
        ):
            raise PostedStockReturnError("Проведённый возврат нельзя изменить.")
        return super().update(**kwargs)

    def delete(self):
        if self._posted():
            raise PostedStockReturnError("Проведённый возврат нельзя удалить.")
        return super().delete()


class StockReturnManager(models.Manager.from_queryset(StockReturnQuerySet)):
    def bulk_update(self, objs, fields, batch_size=None):
        if "status" in fields:
            raise PostedStockReturnError("Статус возврата меняется только сервисом.")
        ids = [obj.pk for obj in objs]
        if self.filter(pk__in=ids).exclude(status=DRAFT).exists() and not (
            _cost_correction.get() and set(fields) <= COST_FIELDS_HEADER | _ALWAYS_MUTABLE_HEADER
        ):
            raise PostedStockReturnError("Проведённый возврат нельзя изменить.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        if any(obj.status != DRAFT for obj in objs):
            raise PostedStockReturnError("Возврат создаётся только черновиком.")
        return super().bulk_create(objs, *args, **kwargs)


class StockReturnLineQuerySet(models.QuerySet):
    def _posted(self):
        return self.exclude(stock_return__status=DRAFT).exists()

    def update(self, **kwargs):
        if self._posted() and not (_cost_correction.get() and set(kwargs) <= COST_FIELDS_LINE):
            raise PostedStockReturnError("Строки проведённого возврата нельзя изменить.")
        if "stock_return" in kwargs or "stock_return_id" in kwargs:
            raise PostedStockReturnError("Строку нельзя перенести в другой документ.")
        return super().update(**kwargs)

    def delete(self):
        if self._posted():
            raise PostedStockReturnError("Строки проведённого возврата нельзя удалить.")
        return super().delete()


class StockReturnLineManager(models.Manager.from_queryset(StockReturnLineQuerySet)):
    def bulk_update(self, objs, fields, batch_size=None):
        ids = [obj.pk for obj in objs]
        posted = self.filter(pk__in=ids).exclude(stock_return__status=DRAFT).exists()
        if (posted and not (_cost_correction.get() and set(fields) <= COST_FIELDS_LINE)) or (
            "stock_return" in fields
        ):
            raise PostedStockReturnError("Строки проведённого возврата нельзя изменить.")
        return super().bulk_update(objs, fields, batch_size=batch_size)

    def bulk_create(self, objs, *args, **kwargs):
        parents = {obj.stock_return_id for obj in objs}
        if StockReturn._base_manager.filter(pk__in=parents).exclude(status=DRAFT).exists():
            raise PostedStockReturnError("В проведённый возврат нельзя добавить строки.")
        return super().bulk_create(objs, *args, **kwargs)


class StockReturn(models.Model):
    """Документ возврата на склад (Слой 18): физическое обратное поступление
    проданной (Слой 16) или выданной в ремонт (Слой 17) детали.

    Это НЕ денежный refund/чек/сторно: документ возвращает физический остаток и
    порождает приходное движение, но финансовую историю `Sale`/`RepairOrder` не
    меняет и их статус `completed` не трогает. Физическое поступление идёт ТОЛЬКО
    через сервисы `apps.inventory` (`return_part_item`/`return_stock_lot_quantity`):
    сам возврат ledger (`StockMovement`/`StockBalance`/статусы/quantity) не пишет.
    Проведённый возврат отменяется только компенсирующим domain service:
    он повторно списывает физически возвращённое через inventory services,
    блокирует строки и отказывается, если остаток уже использован.

    Возврат оформляется из ОДНОГО документа-источника: `source_type` ∈
    {sale, repair_order}, `source_id` — id `Sale`/`RepairOrder` (лёгкий указатель,
    как `StockMovement.document_*`, без contenttypes).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COMPLETED = "completed", "Проведён"
        CANCELED = "canceled", "Отменён"

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
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто провёл",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    canceled_at = models.DateTimeField("Отменён (когда)", null=True, blank=True)
    canceled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто отменил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    cancel_reason = models.CharField("Причина отмены", max_length=255, blank=True)

    objects = StockReturnManager()

    class Meta:
        verbose_name = "Возврат на склад"
        verbose_name_plural = "Возвраты на склад"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.get_source_type_display()} #{self.source_id})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("stock_return")
        with transaction.atomic():
            self._guard_posted_history(kwargs.get("update_fields"))
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if self.pk and _locked_status(StockReturn, self.pk) not in (None, DRAFT):
                raise PostedStockReturnError("Проведённый возврат нельзя удалить.")
            return super().delete(*args, **kwargs)

    def _guard_posted_history(self, update_fields):
        """Compare against the locked DB row, never against this (maybe stale) instance."""
        db_status = _locked_status(StockReturn, self.pk) if self.pk else None
        if db_status is None:
            if self.status != DRAFT:
                raise PostedStockReturnError("Возврат создаётся только черновиком.")
            return
        names = (
            set(update_fields) if update_fields is not None
            else {f.name for f in self._meta.concrete_fields if not f.primary_key}
        )
        current = type(self)._base_manager.filter(pk=self.pk).values(
            *[self._meta.get_field(n).attname for n in names]
        ).first()
        changed = {
            n for n in names
            if current[self._meta.get_field(n).attname]
            != getattr(self, self._meta.get_field(n).attname)
        } - _ALWAYS_MUTABLE_HEADER
        transition = _status_transition.get()
        allowed_target = transition[1] if transition and transition[0] == self.pk else None
        if db_status == DRAFT:
            if "status" in changed and self.status != allowed_target:
                raise PostedStockReturnError("Статус возврата меняется только сервисом.")
            return
        allowed = set()
        if _cost_correction.get():
            allowed |= COST_FIELDS_HEADER
        if (
            db_status == self.Status.COMPLETED
            and allowed_target == self.Status.CANCELED
            and self.status == self.Status.CANCELED
        ):
            allowed |= _CANCEL_FIELDS
        if changed - allowed:
            raise PostedStockReturnError(
                "Проведённый возврат неизменяем: "
                + ", ".join(sorted(changed - allowed))
            )


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
        default=RestockStatus.AVAILABLE,
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

    objects = StockReturnLineManager()

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

    def save(self, *args, **kwargs):
        with transaction.atomic():
            # Lock the document first (same order as the services) so a line
            # edit and a completion serialize instead of deadlocking.
            parent_status = _locked_status(StockReturn, self.stock_return_id)
            if self.pk:
                previous = type(self)._base_manager.filter(pk=self.pk).values_list(
                    "stock_return_id", flat=True,
                ).first()
                if previous is not None and previous != self.stock_return_id:
                    raise PostedStockReturnError("Строку нельзя перенести в другой документ.")
            if parent_status != DRAFT:
                update_fields = kwargs.get("update_fields")
                if not (
                    self.pk and _cost_correction.get() and update_fields is not None
                    and set(update_fields) <= COST_FIELDS_LINE
                ):
                    raise PostedStockReturnError("Строки проведённого возврата неизменяемы.")
            super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        with transaction.atomic():
            if _locked_status(StockReturn, self.stock_return_id) not in (None, DRAFT):
                raise PostedStockReturnError("Строки проведённого возврата нельзя удалить.")
            return super().delete(*args, **kwargs)
