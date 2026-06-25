from django.conf import settings
from django.db import models

from apps.inventory.models import NumberSequence


class Reservation(models.Model):
    """Шапка коммерческой брони: «отложили для клиента», а не «продали».

    Источник истины о брони — `Reservation` + `ReservationLine` (Слой 15).
    Бронь НЕ создаёт `StockMovement` и НЕ меняет физический остаток: только
    активная бронь уменьшает доступность (`available`), а `StockBalance.
    quantity_reserved` пересобирается из активных строк как кэш. Продажа —
    отдельный будущий слой.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        ACTIVE = "active", "Активен"
        CANCELED = "canceled", "Отменён"
        EXPIRED = "expired", "Просрочен"
        # FUTURE (Слой 16): конверсию в продажу выставит слой продаж; в Слое 15
        # значение определено, но не используется.
        CONVERTED = "converted_to_sale", "Продан"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    customer_name = models.CharField("Клиент", max_length=255)
    customer_phone = models.CharField("Телефон", max_length=50, blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    expires_at = models.DateTimeField("Действует до", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто создал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    canceled_at = models.DateTimeField("Закрыт (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Резерв"
        verbose_name_plural = "Резервы"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.customer_name})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("reservation")
        super().save(*args, **kwargs)


class ReservationLine(models.Model):
    """Позиция брони: конкретный экземпляр (целиком) ИЛИ количество из лота.

    XOR `part_item` / `stock_lot` (как у `StockMovement`). «Активность» строки
    определяется статусом её шапки (`reservation.status == active` и бронь не
    истекла) — отдельного статуса на строке нет, чтобы не было рассинхрона.
    """

    reservation = models.ForeignKey(
        Reservation, verbose_name="Резерв", on_delete=models.CASCADE, related_name="lines"
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    part_item = models.ForeignKey(
        "inventory.PartItem", verbose_name="Экземпляр",
        on_delete=models.PROTECT, null=True, blank=True, related_name="reservation_lines",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот",
        on_delete=models.PROTECT, null=True, blank=True, related_name="reservation_lines",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция резерва"
        verbose_name_plural = "Позиции резерва"
        ordering = ["id"]
        constraints = [
            # Позиция относится либо к экземпляру, либо к лоту — ровно к одному.
            models.CheckConstraint(
                condition=(
                    models.Q(part_item__isnull=False, stock_lot__isnull=True)
                    | models.Q(part_item__isnull=True, stock_lot__isnull=False)
                ),
                name="reservationline_item_xor_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="reservationline_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        target = self.part_item or self.stock_lot
        return f"{self.part_type} × {self.quantity} ({target})"


class Sale(models.Model):
    """Коммерческий документ продажи (Слой 16).

    Впервые порождает физический складской расход, но сам ledger НЕ пишет:
    `apps/sales` ведёт документ (цены/выручку/себестоимость/прибыль, связь с
    резервом), а физическое списание (`PartItem.status`/`StockLot.quantity`,
    `StockMovement`, `StockBalance`) выполняют сервисы `apps/inventory`
    (`sell_part_item`/`sell_stock_lot`). Проведённая продажа неизменяема —
    отмена/возврат/сторно это отдельный будущий слой.
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COMPLETED = "completed", "Проведена"
        # FUTURE (слой возвратов/сторно): определены, в Слое 16 не выставляются.
        CANCELED = "canceled", "Отменена"
        VOIDED = "voided", "Сторнирована"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    customer_name = models.CharField("Клиент", max_length=255)
    customer_phone = models.CharField("Телефон", max_length=50, blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    reservation = models.ForeignKey(
        Reservation, verbose_name="Из резерва",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="sales",
    )
    sold_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто провёл",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    sold_at = models.DateTimeField("Проведена (когда)", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    canceled_at = models.DateTimeField("Закрыта (когда)", null=True, blank=True)
    revenue_total = models.DecimalField(
        "Выручка (₽)", max_digits=14, decimal_places=2, default=0
    )
    cost_total = models.DecimalField(
        "Себестоимость (₽)", max_digits=14, decimal_places=2, default=0
    )
    profit_total = models.DecimalField(
        "Прибыль (₽)", max_digits=14, decimal_places=2, default=0
    )

    class Meta:
        verbose_name = "Продажа"
        verbose_name_plural = "Продажи"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.customer_name})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("sale")
        super().save(*args, **kwargs)


class SaleLine(models.Model):
    """Строка продажи: экземпляр целиком ИЛИ количество из лота (XOR).

    Себестоимость (`unit_cost_rub`/`total_cost_rub`) и прибыль (`profit_rub`)
    замораживаются в момент проведения продажи и не пересчитываются от будущих
    изменений landed cost.
    """

    sale = models.ForeignKey(
        Sale, verbose_name="Продажа", on_delete=models.CASCADE, related_name="lines"
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    part_item = models.ForeignKey(
        "inventory.PartItem", verbose_name="Экземпляр",
        on_delete=models.PROTECT, null=True, blank=True, related_name="sale_lines",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот",
        on_delete=models.PROTECT, null=True, blank=True, related_name="sale_lines",
    )
    batch = models.ForeignKey(
        "procurement.Batch", verbose_name="Партия", on_delete=models.PROTECT, related_name="+"
    )
    batch_line = models.ForeignKey(
        "procurement.BatchLine", verbose_name="Строка партии",
        on_delete=models.PROTECT, related_name="+",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_price = models.DecimalField("Цена за ед. (₽)", max_digits=12, decimal_places=2)
    total_price = models.DecimalField(
        "Сумма (₽)", max_digits=14, decimal_places=2, default=0
    )
    unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    total_cost_rub = models.DecimalField(
        "Себестоимость строки (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    profit_rub = models.DecimalField(
        "Прибыль (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция продажи"
        verbose_name_plural = "Позиции продажи"
        ordering = ["id"]
        constraints = [
            models.CheckConstraint(
                condition=(
                    models.Q(part_item__isnull=False, stock_lot__isnull=True)
                    | models.Q(part_item__isnull=True, stock_lot__isnull=False)
                ),
                name="saleline_item_xor_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="saleline_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        target = self.part_item or self.stock_lot
        return f"{self.part_type} × {self.quantity} ({target})"
