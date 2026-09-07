from decimal import Decimal

from django.conf import settings
from django.db import models


class CustomsOrder(models.Model):
    number = models.PositiveIntegerField("Номер заказа", unique=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True
    )
    fx_rate = models.DecimalField("Курс USD/RUB", max_digits=10, decimal_places=4)
    total_quantity = models.DecimalField(
        "Количество", max_digits=14, decimal_places=3, default=Decimal("0")
    )
    total_rub = models.DecimalField(
        "Сумма, ₽", max_digits=16, decimal_places=2, default=Decimal("0")
    )

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Таможенный заказ №{self.number}"


class CustomsOrderLine(models.Model):
    class Source(models.TextChoices):
        SALE = "sale", "Продажа"
        REPAIR = "repair", "Ремонт"
        ORDERED = "ordered", "Запчасть на заказ"

    order = models.ForeignKey(CustomsOrder, related_name="lines", on_delete=models.PROTECT)
    source = models.CharField(max_length=20, choices=Source.choices)
    source_id = models.PositiveBigIntegerField()
    article = models.CharField(max_length=100, blank=True)
    name_ru = models.CharField(max_length=255, blank=True)
    name_en = models.CharField(max_length=255, blank=True)
    manufacturer = models.CharField(max_length=150, blank=True)
    quantity = models.DecimalField(max_digits=14, decimal_places=3)
    wholesale_usd = models.DecimalField(max_digits=14, decimal_places=4)
    rub_amount = models.DecimalField(max_digits=16, decimal_places=2)
    is_analog = models.BooleanField(default=False)
    is_ordered = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["source", "source_id"], name="customs_order_line_unique_source"
            ),
        ]
        ordering = ["pk"]

    def __str__(self):
        return f"{self.article} в заказе №{self.order.number}"
