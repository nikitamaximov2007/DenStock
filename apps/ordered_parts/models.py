"""Запчасти, заказанные клиентом, но ещё не находящиеся на складе.

Клиент звонит и просит привезти ему ОРИГИНАЛЬНУЮ деталь из-за границы, а
затем переводит предоплату. Такой детали на складе может не быть вовсе: её
только предстоит купить. Значит, зацепиться за лот, ячейку или движение
нельзя - их ещё не существует.

Поэтому запись опирается на КАТАЛОГ, а не на остаток: деталь выбрана из уже
загруженных каталогов (BRP, Polaris, каталог аналогов), и с ней сохранён
снимок её личности на момент заказа. Каталог завтра обновится - заказ
останется читаемым, потому что артикул, название и производитель записаны
здесь, а не вычисляются заново.

Чего здесь СОЗНАТЕЛЬНО нет:

* количества. Пользователь его не просил: одна запись - одна заказанная
  единица. Две одинаковые детали - две записи;
* статусов жизненного цикла. «Оформлен / Заказан / Получен / Выдан» звучат
  логично, но не утверждены, а придуманный статус потом дороже, чем его
  отсутствие;
* любой связи со складом. Заказ не создаёт ни лота, ни движения, ни продажи,
  ни приёмки: он фиксирует обещание клиенту, а не остаток.

Предоплата - справочная сумма именно этого заказа. Это НЕ себестоимость, НЕ
цена продажи и НЕ таможенная стоимость: в декларацию уходит цена из каталога,
как и у обычных деталей.
"""
from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models


class OrderedPart(models.Model):
    """Одна заказанная клиентом оригинальная деталь."""

    customer = models.ForeignKey(
        "customers.Customer", verbose_name="Клиент",
        on_delete=models.PROTECT, related_name="ordered_parts",
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь",
        on_delete=models.PROTECT, related_name="ordered_parts",
    )
    # Снимок личности детали на момент заказа - тот же приём, что у
    # WarehouseAction. Именно этот артикул уйдёт в таможенную форму: сегодняшний
    # номер карточки историей заказа не является.
    article = models.CharField("Артикул (снимок)", max_length=100)
    part_name = models.CharField("Название (снимок)", max_length=255, blank=True)
    manufacturer_name = models.CharField("Производитель (снимок)", max_length=120, blank=True)
    # Каким каталогом деталь доказана: этого достаточно, чтобы позже показать,
    # откуда она взялась, не копируя сюда весь каталог.
    catalog_source = models.CharField("Источник каталога (снимок)", max_length=40, blank=True)
    prepayment_rub = models.DecimalField(
        "Предоплата (₽)", max_digits=12, decimal_places=2,
        default=Decimal("0"), validators=[MinValueValidator(Decimal("0"))],
    )
    created_at = models.DateTimeField("Создан", auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто оформил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто изменил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Запчасть на заказ"
        verbose_name_plural = "Запчасти на заказ"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(prepayment_rub__gte=Decimal("0")),
                name="orderedpart_prepayment_not_negative",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.article} для {self.customer_id}"
