"""Layer 31 — BRP-каталог: СПРАВОЧНИК дилерского прайса, НЕ складской остаток.

Ключевое архитектурное правило: импорт каталога (127 тысяч строк) не создаёт
ни остатков, ни движений, ни поступлений. На складе появляются только детали,
которые физически есть у Дениса: пользователь находит позицию в BRP-каталоге,
«продвигает» её в карточку склада (BrpPartLink) и учитывает наличие обычным
документом поступления.

Цены прайса в долларах. Цена клиента считается по формуле и округляется до
ЦЕЛОГО рубля (ROUND_HALF_UP, без копеек); исходные USD, курс и наценка не
округляются:
    цена_клиента_руб = округлить(розница_USD * курс * (1 + наценка_% / 100))
Курс и наценка настраиваются (BrpPricingSettings); при продвижении детали
использованные курс/наценка фиксируются в BrpPartLink и задним числом не
меняются.
"""
from decimal import Decimal

from django.conf import settings as django_settings
from django.db import models

from apps.catalog.models import normalize_number


class BrpCatalogPart(models.Model):
    """Одна строка дилерского прайса BRP. Только справочник."""

    material_no = models.CharField("Номер BRP (Material No)", max_length=40, unique=True)
    material_no_norm = models.CharField(max_length=40, editable=False, db_index=True)
    part_desc = models.CharField("Описание BRP", max_length=255, blank=True)
    last_year_util = models.CharField("Последний год использования", max_length=20, blank=True)
    brp_status = models.CharField("Статус BRP", max_length=20, blank=True, db_index=True)
    retail_price_usd = models.DecimalField(
        "Розница BRP (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    wholesale_price_usd = models.DecimalField(
        "Оптовая BRP (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    replacement_no_1 = models.CharField("Замена номера 1", max_length=40, blank=True)
    replacement_no_1_norm = models.CharField(max_length=40, editable=False, db_index=True)
    replacement_no_2 = models.CharField("Замена номера 2", max_length=40, blank=True)
    replacement_no_2_norm = models.CharField(max_length=40, editable=False, db_index=True)
    source_file = models.CharField("Файл импорта", max_length=255, blank=True)
    source_row = models.PositiveIntegerField("Строка в файле", null=True, blank=True)
    import_batch = models.CharField("Партия импорта", max_length=40, blank=True)
    imported_at = models.DateTimeField("Импортировано", auto_now_add=True)
    updated_at = models.DateTimeField("Обновлено", auto_now=True)

    # Расшифровка статусов из легенды файла. UCP не расшифровываем, пока Денис
    # не подтвердит значение: показываем сырой статус.
    STATUS_LABELS = {
        "OBS": "снято с производства",
        "USE": "замена номера",
        "VIN": "винтажный склад, будет доставка 25$",
        "LIQ": "последние остатки у завода",
    }

    class Meta:
        verbose_name = "Позиция BRP-каталога"
        verbose_name_plural = "Позиции BRP-каталога"
        ordering = ["material_no"]

    def __str__(self) -> str:
        return f"{self.material_no} {self.part_desc}".strip()

    def save(self, *args, **kwargs):
        self.material_no_norm = normalize_number(self.material_no)
        self.replacement_no_1_norm = normalize_number(self.replacement_no_1)
        self.replacement_no_2_norm = normalize_number(self.replacement_no_2)
        super().save(*args, **kwargs)

    @property
    def status_label(self) -> str:
        if not self.brp_status:
            return ""
        hint = self.STATUS_LABELS.get(self.brp_status)
        return f"{self.brp_status}: {hint}" if hint else self.brp_status


class BrpPricingSettings(models.Model):
    """BRP markup settings (one row). The USD rate is shared in ValuationSettings.

    Изменение настроек влияет ТОЛЬКО на будущие расчёты: у уже продвинутых
    деталей курс/наценка зафиксированы в BrpPartLink.
    """

    brp_markup_percent = models.DecimalField(
        "Наценка, %", max_digits=6, decimal_places=2, default=Decimal("40")
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        django_settings.AUTH_USER_MODEL, verbose_name="Кто изменил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Настройки цен BRP"
        verbose_name_plural = "Настройки цен BRP"

    def __str__(self) -> str:
        return f"наценка BRP {self.brp_markup_percent}%"

    @classmethod
    def get(cls) -> "BrpPricingSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class BrpPartLink(models.Model):
    """Связь карточки склада с позицией BRP-каталога + снимок расчёта цены.

    Снимок (retail/wholesale USD, использованные курс и наценка, рассчитанная
    и итоговая цена) фиксируется в момент продвижения и не меняется при смене
    глобальных настроек: история честная.
    """

    class PriceSource(models.TextChoices):
        CALCULATED = "calculated", "Рассчитана по формуле"
        MANUAL = "manual", "Указана вручную"

    part = models.OneToOneField(
        "catalog.PartType", verbose_name="Карточка склада",
        on_delete=models.CASCADE, related_name="brp_link",
    )
    brp_part = models.ForeignKey(
        BrpCatalogPart, verbose_name="Позиция BRP",
        on_delete=models.PROTECT, related_name="links",
    )
    brp_retail_price_usd = models.DecimalField(
        "Розница BRP (USD)", max_digits=12, decimal_places=2, null=True, blank=True
    )
    brp_wholesale_price_usd = models.DecimalField(
        "Оптовая BRP (USD)", max_digits=12, decimal_places=2, null=True, blank=True
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
        verbose_name = "Связь склада с BRP-каталогом"
        verbose_name_plural = "Связи склада с BRP-каталогом"

    def __str__(self) -> str:
        return f"{self.part} <- BRP {self.brp_part.material_no}"
