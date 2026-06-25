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
