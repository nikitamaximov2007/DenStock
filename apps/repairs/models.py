from django.conf import settings
from django.db import models

from apps.inventory.models import NumberSequence


class RepairOrder(models.Model):
    """Ремонтный заказ (Слой 17): выдача/установка деталей на технику клиента.

    Это НЕ продажа и НЕ касса: документ фиксирует, КУДА (на какую технику) и
    ЗАЧЕМ (какой ремонт) ушли детали, и замораживает их себестоимость. Денежной
    стороны (цена работ, оплата, чек, прибыль) здесь нет.

    Физическое выбытие происходит при проведении (`draft → completed`) и идёт
    ТОЛЬКО через сервисы `apps.inventory` (`issue_part_item`/`issue_stock_lot`):
    сам ремонт ledger (`StockMovement`/`StockBalance`/статусы) не пишет.
    Проведённый заказ неизменяем; возврат установленной детали — будущий слой.

    Клиент и техника — без CRM/автопарка: клиент текстом, тип техники —
    опциональный справочник `catalog.VehicleType`, марка/модель/VIN — свободный
    текст (конкретная машина клиента произвольна).
    """

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        COMPLETED = "completed", "Проведён"
        CANCELED = "canceled", "Отменён"

    number = models.CharField("Номер", max_length=20, unique=True, editable=False)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    customer_name = models.CharField("Клиент", max_length=255)
    customer_phone = models.CharField("Телефон", max_length=50, blank=True)
    vehicle_type = models.ForeignKey(
        "catalog.VehicleType", verbose_name="Вид техники",
        on_delete=models.PROTECT, null=True, blank=True, related_name="+",
    )
    vehicle_make = models.CharField("Марка техники", max_length=120, blank=True)
    vehicle_model = models.CharField("Модель техники", max_length=150, blank=True)
    vehicle_identifier = models.CharField("VIN / серийный № техники", max_length=100, blank=True)
    problem_description = models.TextField("Что ремонтируем", blank=True)
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    cost_total = models.DecimalField(
        "Себестоимость выданного (₽)", max_digits=14, decimal_places=2, default=0
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
        verbose_name = "Ремонтный заказ"
        verbose_name_plural = "Ремонтные заказы"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.number} ({self.customer_name})"

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = NumberSequence.next("repair_order")
        super().save(*args, **kwargs)


class RepairIssueLine(models.Model):
    """Строка выдачи в ремонт: экземпляр целиком ИЛИ количество из лота (XOR).

    Себестоимость (`unit_cost_rub`/`total_cost_rub`) замораживается в момент
    проведения заказа и не пересчитывается от будущих изменений landed cost.
    Цены/прибыли здесь нет — это складской расход, а не продажа.
    """

    repair_order = models.ForeignKey(
        RepairOrder, verbose_name="Ремонтный заказ",
        on_delete=models.CASCADE, related_name="lines",
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь", on_delete=models.PROTECT, related_name="+"
    )
    part_item = models.ForeignKey(
        "inventory.PartItem", verbose_name="Экземпляр",
        on_delete=models.PROTECT, null=True, blank=True, related_name="repair_lines",
    )
    stock_lot = models.ForeignKey(
        "inventory.StockLot", verbose_name="Лот",
        on_delete=models.PROTECT, null=True, blank=True, related_name="repair_lines",
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
    issued_at = models.DateTimeField("Выдано (когда)", null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "Позиция выдачи в ремонт"
        verbose_name_plural = "Позиции выдачи в ремонт"
        ordering = ["id"]
        constraints = [
            # Позиция относится либо к экземпляру, либо к лоту — ровно к одному.
            models.CheckConstraint(
                condition=(
                    models.Q(part_item__isnull=False, stock_lot__isnull=True)
                    | models.Q(part_item__isnull=True, stock_lot__isnull=False)
                ),
                name="repairissueline_item_xor_lot",
            ),
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="repairissueline_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        target = self.part_item or self.stock_lot
        return f"{self.part_type} × {self.quantity} ({target})"
