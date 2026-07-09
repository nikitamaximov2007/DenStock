"""Layer 33 — быстрые действия со склада (сканер) и таможенные данные деталей.

Разделение слоёв: WarehouseAction — журнальная запись для единого отчёта и
таможенного экспорта. Сама физика склада НЕ здесь: продажа/резерв/ремонт
проводятся существующими сервисами apps.sales / apps.repairs, которые пишут
движения и остатки. Каждое действие ссылается на созданный документ, поэтому
след аудита двойной: документ + движения ledger.

PartCustomsInfo — таможенная карточка детали для экспорта «Формы для заказа»:
русское название (ручное или автоперевод), веса брутто/нетто (ТОЛЬКО ручные
или с проверенным источником — никогда не выдумываются), страна и область
применения. Одна запись на карточку детали (OneToOne), BRP-каталог не мутируем.
"""
from django.conf import settings
from django.db import models


class WarehouseAction(models.Model):
    """Одно проведённое действие со сканера: продажа, резерв или ремонт.

    Личность детали фиксируется СНИМКОМ при создании (`part_number`,
    `part_name`, `location_code`): именно этот номер показывается в отчёте,
    таможенном блоке и Excel. Отображаемый номер — ТОЧНО тот, что сканировали/
    продавали; никакие замены, источники цены и соседние номера его не
    подменяют (`price_source_number` — только аудит источника цены).
    """

    class Type(models.TextChoices):
        SALE = "sale", "Продажа"
        RESERVE = "reserve", "Резерв"
        REPAIR = "repair", "Ремонт"

    class Status(models.TextChoices):
        ACTIVE = "active", "Проведено"
        CANCELLED = "cancelled", "Отменено"

    action_type = models.CharField("Тип", max_length=10, choices=Type.choices)
    status = models.CharField(
        "Статус", max_length=10, choices=Status.choices, default=Status.ACTIVE, db_index=True
    )
    part_type = models.ForeignKey(
        "catalog.PartType", verbose_name="Деталь",
        on_delete=models.PROTECT, related_name="warehouse_actions",
    )
    # Снимок личности детали на момент действия (номер, который сканировали).
    part_number = models.CharField("Номер детали (снимок)", max_length=100, blank=True)
    part_name = models.CharField("Название (снимок)", max_length=255, blank=True)
    manufacturer_name = models.CharField(
        "Производитель (снимок)", max_length=80, blank=True
    )
    location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Ячейка списания",
        on_delete=models.PROTECT, related_name="warehouse_actions",
    )
    location_code = models.CharField("Ячейка (снимок)", max_length=80, blank=True)
    quantity = models.DecimalField("Количество", max_digits=12, decimal_places=3)
    unit_price_rub = models.DecimalField(
        "Цена клиента за ед. (₽)", max_digits=12, decimal_places=2, default=0
    )
    total_price_rub = models.DecimalField(
        "Сумма (₽)", max_digits=14, decimal_places=2, default=0
    )
    # Если цена взята из связанной замены (правило 32.3.2), её номер — для
    # аудита. Номер ПРОДАННОЙ детали (part_number) от этого не меняется.
    price_source_number = models.CharField(
        "Номер источника цены", max_length=100, blank=True
    )
    customer_comment = models.CharField("Клиент / комментарий", max_length=255)
    sale = models.ForeignKey(
        "sales.Sale", verbose_name="Продажа", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="scanner_actions", editable=False,
    )
    reservation = models.ForeignKey(
        "sales.Reservation", verbose_name="Резерв", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="scanner_actions", editable=False,
    )
    repair_order = models.ForeignKey(
        "repairs.RepairOrder", verbose_name="Ремонтный заказ",
        on_delete=models.SET_NULL,
        null=True, blank=True, related_name="scanner_actions", editable=False,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто провёл",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField("Проведено", auto_now_add=True)
    cancelled_at = models.DateTimeField("Отменено (когда)", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто отменил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    cancel_reason = models.CharField("Причина отмены", max_length=255, blank=True)

    class Meta:
        verbose_name = "Действие со склада"
        verbose_name_plural = "Действия со склада"
        ordering = ["-created_at", "-id"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(quantity__gt=0), name="warehouseaction_qty_positive"
            ),
        ]

    def __str__(self) -> str:
        number = self.part_number or self.part_type
        return f"{self.get_action_type_display()} {number} x {self.quantity}"

    @property
    def document(self):
        """Связанный документ (для ссылки из отчёта)."""
        return self.sale or self.reservation or self.repair_order

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED


class PartCustomsInfo(models.Model):
    """Таможенные данные карточки детали (для экспорта «Формы для заказа»)."""

    class NameSource(models.TextChoices):
        MANUAL = "manual", "Введено вручную"
        AUTO = "auto_translation", "Автоперевод"

    part_type = models.OneToOneField(
        "catalog.PartType", verbose_name="Деталь",
        on_delete=models.CASCADE, related_name="customs_info",
    )
    customs_name_ru = models.CharField("Таможенное название (RU)", max_length=255, blank=True)
    customs_name_source = models.CharField(
        "Источник названия", max_length=20,
        choices=NameSource.choices, default=NameSource.AUTO,
    )
    manufacturer = models.CharField("Производитель", max_length=80, default="BRP")
    country_of_origin = models.CharField("Страна производства", max_length=80, default="КАНАДА")
    gross_weight_kg = models.DecimalField(
        "Вес брутто, кг/шт", max_digits=8, decimal_places=3, null=True, blank=True
    )
    net_weight_kg = models.DecimalField(
        "Вес нетто, кг/шт", max_digits=8, decimal_places=3, null=True, blank=True
    )
    weight_source_url = models.URLField("Источник веса (URL)", blank=True)
    weight_source_note = models.CharField("Источник веса (примечание)", max_length=255, blank=True)
    weight_verified = models.BooleanField("Вес проверен", default=False)
    application_area = models.CharField(
        "Область применения", max_length=120, default="МОТО ЗАПЧАСТИ"
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто изменил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Таможенные данные детали"
        verbose_name_plural = "Таможенные данные деталей"

    def __str__(self) -> str:
        return f"Таможенные данные: {self.part_type}"
