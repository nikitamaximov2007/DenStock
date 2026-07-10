"""Polaris catalog models.

The catalog is reference data only. Importing Polaris rows must not create
stock, movements, receipts, sales, or warehouse balances.
"""
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import models

from apps.catalog.models import normalize_number


class PolarisCatalogPart(models.Model):
    """One row from the Polaris dealer price file."""

    part_number = models.CharField("Номер Polaris", max_length=40, unique=True)
    part_number_norm = models.CharField(max_length=40, editable=False, db_index=True)
    part_name = models.CharField("Название Polaris", max_length=255, blank=True)
    superseded_number = models.CharField("Superseded number", max_length=40, blank=True)
    superseded_number_norm = models.CharField(max_length=40, editable=False, db_index=True)
    wholesale_price_usd = models.DecimalField(
        "Оптовая Polaris (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    retail_price_usd = models.DecimalField(
        "Розница Polaris (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    uom = models.CharField("UOM", max_length=40, blank=True)
    source_file = models.CharField("Файл импорта", max_length=255, blank=True)
    source_row = models.PositiveIntegerField("Строка в файле", null=True, blank=True)
    import_batch = models.CharField("Партия импорта", max_length=40, blank=True)
    imported_at = models.DateTimeField("Импортировано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    class Meta:
        verbose_name = "Позиция Polaris-каталога"
        verbose_name_plural = "Позиции Polaris-каталога"
        ordering = ["part_number"]
        indexes = [
            models.Index(fields=["part_name"], name="polaris_part_name_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.part_number} {self.part_name}".strip()

    def save(self, *args, **kwargs):
        self.part_number_norm = normalize_number(self.part_number)
        self.superseded_number_norm = normalize_number(self.superseded_number)
        super().save(*args, **kwargs)


class PolarisPricingSettings(models.Model):
    """Separate Polaris markup settings. The USD rate is shared in ValuationSettings."""

    polaris_markup_percent = models.DecimalField(
        "Наценка, %", max_digits=6, decimal_places=2, default=Decimal("40")
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, verbose_name="Кто изменил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Настройки цен Polaris"
        verbose_name_plural = "Настройки цен Polaris"

    def __str__(self) -> str:
        return f"наценка Polaris {self.polaris_markup_percent}%"

    @classmethod
    def get(cls) -> "PolarisPricingSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class PolarisPartLink(models.Model):
    """Warehouse part linked to a Polaris catalog row with a price snapshot."""

    class PriceSource(models.TextChoices):
        CALCULATED = "calculated", "Рассчитана по формуле"
        MANUAL = "manual", "Указана вручную"

    part = models.OneToOneField(
        "catalog.PartType", verbose_name="Карточка склада",
        on_delete=models.CASCADE, related_name="polaris_link",
    )
    polaris_part = models.ForeignKey(
        PolarisCatalogPart, verbose_name="Позиция Polaris",
        on_delete=models.PROTECT, related_name="links",
    )
    polaris_retail_price_usd = models.DecimalField(
        "Розница Polaris (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    polaris_wholesale_price_usd = models.DecimalField(
        "Оптовая Polaris (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    usd_rate_used = models.DecimalField(
        "Курс на момент расчёта", max_digits=10, decimal_places=4
    )
    markup_percent_used = models.DecimalField(
        "Наценка на момент расчёта, %", max_digits=6, decimal_places=2
    )
    calculated_customer_price_rub = models.DecimalField(
        "Рассчитанная цена клиента (₽)", max_digits=16, decimal_places=6,
        null=True, blank=True,
    )
    manual_customer_price_rub = models.DecimalField(
        "Цена клиента вручную (₽)", max_digits=16, decimal_places=6,
        null=True, blank=True,
    )
    final_customer_price_rub = models.DecimalField(
        "Итоговая цена клиента (₽)", max_digits=16, decimal_places=6,
        null=True, blank=True,
    )
    price_source = models.CharField(
        "Источник цены", max_length=20,
        choices=PriceSource.choices, default=PriceSource.CALCULATED,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, verbose_name="Кто добавил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Связь склада с Polaris-каталогом"
        verbose_name_plural = "Связи склада с Polaris-каталогом"

    def __str__(self) -> str:
        return f"{self.part} <- POLARIS {self.polaris_part.part_number}"

