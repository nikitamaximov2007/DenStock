from decimal import Decimal

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class FrozenSnapshot(models.Model):
    """Orders are finalized snapshots; there is no edit/delete workflow."""

    class Meta:
        abstract = True

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("Сформированный таможенный заказ нельзя изменять.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("Сформированный таможенный заказ нельзя удалять.")


class CustomsOrder(FrozenSnapshot):
    class OrderType(models.TextChoices):
        ORIGINAL = "original", "Оригиналы"
        ANALOG = "analog", "Аналоги"

    order_type = models.CharField(
        "Тип заказа", max_length=20, choices=OrderType.choices, default=OrderType.ORIGINAL
    )
    number = models.PositiveIntegerField("Номер заказа")
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
        constraints = [
            models.UniqueConstraint(
                fields=["order_type", "number"], name="customs_order_unique_type_number"
            ),
            models.CheckConstraint(
                condition=models.Q(number__gt=0), name="customs_order_number_gt0"
            ),
            models.CheckConstraint(
                condition=models.Q(fx_rate__gt=0), name="customs_order_fx_gt0"
            ),
        ]

    def __str__(self):
        return f"{self.get_order_type_display()} - заказ №{self.number}"


class CustomsOrderLine(FrozenSnapshot):
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
    country = models.CharField(max_length=150, blank=True)
    gross_weight_kg = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    net_weight_kg = models.DecimalField(max_digits=8, decimal_places=3, null=True, blank=True)
    application_area = models.CharField(max_length=120, blank=True)
    occurred_at = models.DateTimeField(null=True, blank=True)
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
            models.CheckConstraint(condition=models.Q(quantity__gt=0), name="customs_line_qty_gt0"),
            models.CheckConstraint(
                condition=models.Q(source__in=["sale", "repair", "ordered"]),
                name="customs_line_valid_source",
            ),
        ]
        ordering = ["pk"]

    def __str__(self):
        return f"{self.article} в заказе №{self.order.number}"
