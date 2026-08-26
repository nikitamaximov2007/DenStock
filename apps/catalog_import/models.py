"""Партия импорта каталога: аудит того, какой файл и когда изменил справочник.

СТРОГО справочник. Импорт каталога не создаёт складских движений, не меняет
остатки, лоты, ячейки, резервы и уже проведённые документы. Здесь хранится
только идентичность файла и результат его разбора.

Почему отдельная модель, а не запись в журнале: у импорта есть жизненный цикл
из двух шагов (проверка и применение), между которыми файл и состояние
каталога обязаны совпасть. Без собственной сущности это состояние негде
держать.

Детальные строки изменений хранятся выборкой, а не целиком: у прайса
BRP порядка 130 тысяч строк, и подавляющее большинство из них не меняется.
Неизменившиеся считаются агрегатно, детально сохраняются только созданные,
изменённые, предупреждения и ошибки.
"""

from django.conf import settings
from django.db import models


class CatalogImportBatch(models.Model):
    """Одна загрузка прайс-листа поставщика."""

    class Catalog(models.TextChoices):
        BRP = "brp", "BRP"
        ANALOGS = "analogs", "Аналоги"
        AFTERMARKET = "aftermarket", "Каталог аналогов / aftermarket"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "Загружен"
        CHECKED = "checked", "Проверен"
        CHECK_FAILED = "check_failed", "Проверка не прошла"
        APPLIED = "applied", "Применён"
        APPLY_FAILED = "apply_failed", "Применение не прошло"

    catalog = models.CharField(
        "Каталог", max_length=20, choices=Catalog.choices, default=Catalog.BRP, db_index=True
    )
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.UPLOADED, db_index=True
    )
    source_filename = models.CharField("Имя файла", max_length=255)
    source_sha256 = models.CharField("SHA-256 файла", max_length=64, db_index=True)
    source_size = models.PositiveBigIntegerField("Размер файла", default=0)
    # Путь внутри приватного хранилища. Коммерческий прайс не публикуется.
    stored_path = models.CharField("Файл в приватном хранилище", max_length=255, blank=True)

    # Слепок состояния каталога на момент проверки. Применение обязано увидеть
    # тот же слепок, иначе предпросмотр устарел.
    catalog_fingerprint = models.CharField("Слепок каталога", max_length=64, blank=True)

    summary = models.JSONField("Сводка проверки", default=dict, blank=True)
    apply_summary = models.JSONField("Сводка применения", default=dict, blank=True)
    error_text = models.TextField("Ошибка", blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто загрузил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField("Загружен", auto_now_add=True)
    checked_at = models.DateTimeField("Проверен (когда)", null=True, blank=True)
    applied_at = models.DateTimeField("Применён (когда)", null=True, blank=True)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Кто применил",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        verbose_name = "Импорт каталога"
        verbose_name_plural = "Импорты каталога"
        ordering = ["-created_at", "-pk"]
        indexes = [
            models.Index(fields=["catalog", "-created_at"], name="catalogimport_recent_idx"),
        ]

    def __str__(self) -> str:
        return f"{self.get_catalog_display()} {self.source_filename}"

    @property
    def can_apply(self) -> bool:
        """Применять можно только успешно проверенную и ещё не применённую партию."""
        return self.status == self.Status.CHECKED

    @property
    def is_applied(self) -> bool:
        return self.status == self.Status.APPLIED

    def counter(self, name: str, default=0):
        """Значение счётчика из сводки проверки (сводка это JSON от импортёра)."""
        return (self.summary or {}).get(name, default)


class AftermarketCatalogPart(models.Model):
    """Current supplier-catalog facts for an independent aftermarket part.

    These USD values are source data, not warehouse cost and not a RUB selling
    price.  Stock and historical documents deliberately have no relation to
    this model.
    """

    SOURCE_DEALER_2023 = "dealer_2023"
    SOURCE_CHOICES = ((SOURCE_DEALER_2023, "Dealer 2023"),)

    source = models.CharField("Источник каталога", max_length=40, choices=SOURCE_CHOICES)
    part = models.OneToOneField(
        "catalog.PartType", on_delete=models.PROTECT, related_name="aftermarket_catalog_entry"
    )
    manufacturer = models.ForeignKey("catalog.Manufacturer", on_delete=models.PROTECT)
    manufacturer_number = models.CharField("Номер производителя", max_length=100)
    normalized_manufacturer_number = models.CharField(max_length=100, db_index=True)
    supplier_sku = models.CharField("SKU поставщика", max_length=100, blank=True)
    source_description = models.CharField("Описание поставщика", max_length=200)
    msrp_usd = models.DecimalField(
        "MSRP, USD", max_digits=14, decimal_places=2, null=True, blank=True
    )
    dealer_cost_usd = models.DecimalField(
        "Dlr Cost, USD", max_digits=14, decimal_places=2, null=True, blank=True
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Позиция aftermarket-каталога"
        verbose_name_plural = "Позиции aftermarket-каталога"
        constraints = [
            models.UniqueConstraint(
                fields=["source", "manufacturer", "normalized_manufacturer_number"],
                name="uniq_aftermarket_source_manufacturer_number",
            )
        ]

    def __str__(self) -> str:
        return f"{self.manufacturer}: {self.manufacturer_number}"

    def save(self, *args, **kwargs):
        from apps.catalog.models import normalize_number

        self.normalized_manufacturer_number = normalize_number(self.manufacturer_number)
        super().save(*args, **kwargs)
