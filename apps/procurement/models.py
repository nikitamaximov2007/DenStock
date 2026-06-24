from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings
from django.db import models, transaction

KOPECKS = Decimal("0.01")


def money(value) -> Decimal:
    """Округление денежной суммы до копеек (Decimal, не float)."""
    return Decimal(value).quantize(KOPECKS, rounding=ROUND_HALF_UP)


def generate_batch_number() -> str:
    """Номер партии вида П-000001 без дублей.

    Атомарность обеспечивается блокировкой последней строки. Полноценный
    атомарный счётчик (NumberSequence) появится позже, когда нумерация
    понадобится нескольким сущностям.
    """
    with transaction.atomic():
        last = Batch.objects.select_for_update().order_by("-number").first()
        next_n = 1
        if last and last.number.startswith("П-"):
            try:
                next_n = int(last.number.split("-")[1]) + 1
            except (IndexError, ValueError):
                next_n = Batch.objects.count() + 1
        return f"П-{next_n:06d}"


class Batch(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Создана"
        ORDERED = "ordered", "Заказана"
        IN_TRANSIT = "in_transit", "В пути"
        ARRIVED = "arrived", "Прибыла"
        RECEIVING = "receiving", "Принимается"
        ACCEPTED = "accepted", "Принята"
        # Будущие статусы (Слой 7) — заложены в choices, но переходы и landed
        # cost на этом слое не реализуются.
        COST_CALCULATED = "cost_calculated", "Себестоимость рассчитана"
        CLOSED = "closed", "Закрыта"
        CANCELED = "canceled", "Отменена"

    class AllocationMethod(models.TextChoices):
        BY_VALUE = "by_value", "По стоимости"
        BY_QUANTITY = "by_quantity", "По количеству"

    # Разрешённые переходы на этом слое (без cost_calculated/closed).
    # Переход accepted → cost_calculated выполняется отдельным действием
    # «Рассчитать себестоимость» (services.finalize_cost), а не сменой статуса.
    ALLOWED_TRANSITIONS = {
        Status.DRAFT: [Status.ORDERED, Status.CANCELED],
        Status.ORDERED: [Status.IN_TRANSIT, Status.CANCELED],
        Status.IN_TRANSIT: [Status.ARRIVED, Status.CANCELED],
        Status.ARRIVED: [Status.RECEIVING, Status.CANCELED],
        Status.RECEIVING: [Status.ACCEPTED],
        Status.ACCEPTED: [],
    }

    number = models.CharField("Номер", max_length=20, unique=True, blank=True)
    supplier = models.ForeignKey(
        "suppliers.Supplier", verbose_name="Поставщик", on_delete=models.PROTECT,
        related_name="batches",
    )
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    country = models.CharField("Страна", max_length=100, blank=True)
    currency = models.CharField("Валюта", max_length=3, default="RUB")
    exchange_rate = models.DecimalField("Курс", max_digits=12, decimal_places=4, default=1)
    order_number = models.CharField("Номер заказа", max_length=100, blank=True)
    invoice_number = models.CharField("Номер счёта", max_length=100, blank=True)
    ordered_at = models.DateField("Дата заказа", null=True, blank=True)
    shipped_at = models.DateField("Дата отправки", null=True, blank=True)
    arrived_at = models.DateField("Дата прибытия", null=True, blank=True)
    notes = models.TextField("Комментарий", blank=True)
    cost_finalized = models.BooleanField("Себестоимость зафиксирована", default=False)
    goods_total = models.DecimalField("Сумма товаров", max_digits=14, decimal_places=2, default=0)
    shipping_cost = models.DecimalField("Доставка", max_digits=14, decimal_places=2, default=0)
    customs_cost = models.DecimalField("Таможня", max_digits=14, decimal_places=2, default=0)
    commission_cost = models.DecimalField("Комиссии", max_digits=14, decimal_places=2, default=0)
    other_cost = models.DecimalField("Прочие расходы", max_digits=14, decimal_places=2, default=0)
    total_extra_cost = models.DecimalField(
        "Сумма доп. расходов", max_digits=14, decimal_places=2, default=0
    )
    cost_allocation_method = models.CharField(
        "Метод распределения", max_length=20,
        choices=AllocationMethod.choices, default=AllocationMethod.BY_VALUE,
    )
    cost_finalized_at = models.DateTimeField(
        "Себестоимость зафиксирована (когда)", null=True, blank=True
    )
    cost_finalized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+", verbose_name="Кто зафиксировал",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Партия"
        verbose_name_plural = "Партии"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.supplier})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = generate_batch_number()
        self.total_extra_cost = money(
            (self.shipping_cost or 0)
            + (self.customs_cost or 0)
            + (self.commission_cost or 0)
            + (self.other_cost or 0)
        )
        super().save(*args, **kwargs)

    @property
    def is_available_for_sale(self) -> bool:
        # Истинно только после фиксации себестоимости (Слой 7). Продаж и
        # остатков ещё нет — это только индикатор готовности партии.
        return self.cost_finalized

    @property
    def costs_editable(self) -> bool:
        # Расходы партии можно править до фиксации себестоимости.
        return not self.cost_finalized

    @property
    def lines_editable(self) -> bool:
        return self.status in {
            self.Status.DRAFT,
            self.Status.ORDERED,
            self.Status.IN_TRANSIT,
            self.Status.ARRIVED,
            self.Status.RECEIVING,
        }

    @property
    def lines_deletable(self) -> bool:
        return self.status == self.Status.DRAFT

    def can_transition_to(self, new_status: str) -> bool:
        return new_status in self.ALLOWED_TRANSITIONS.get(self.status, [])


class BatchLine(models.Model):
    batch = models.ForeignKey(Batch, on_delete=models.CASCADE, related_name="lines")
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT,
        related_name="batch_lines",
    )
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_cost_currency = models.DecimalField(
        "Цена за ед. (валюта)", max_digits=12, decimal_places=2
    )
    unit_cost_rub = models.DecimalField(
        "Цена за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    total_cost_currency = models.DecimalField(
        "Итого (валюта)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    total_cost_rub = models.DecimalField(
        "Итого (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    # Себестоимость с накладными (landed cost) — заполняется на Слое 7 при
    # фиксации; до фиксации хранит нули.
    allocated_overhead_rub = models.DecimalField(
        "Доля доп. расходов (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    landed_unit_cost_rub = models.DecimalField(
        "Себестоимость за ед. (₽)", max_digits=12, decimal_places=2, editable=False, default=0
    )
    landed_total_cost_rub = models.DecimalField(
        "Себестоимость строки (₽)", max_digits=14, decimal_places=2, editable=False, default=0
    )
    note = models.CharField("Примечание", max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Строка поступления"
        verbose_name_plural = "Строки поступления"
        ordering = ["id"]

    def __str__(self) -> str:
        return f"{self.part_type} × {self.quantity}"

    def save(self, *args, **kwargs):
        rate = self.batch.exchange_rate or Decimal(1)
        # Базовая закупочная цена (без накладных). landed cost — Слой 7.
        self.unit_cost_rub = money(self.unit_cost_currency * rate)
        self.total_cost_currency = money(self.quantity * self.unit_cost_currency)
        self.total_cost_rub = money(self.quantity * self.unit_cost_rub)
        super().save(*args, **kwargs)
