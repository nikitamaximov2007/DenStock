"""Layer 32 — быстрая инвентаризация ячейки сканером.

Сессия пересчёта: выбрали адрес -> отпикали всё подряд -> система сгруппировала
одинаковые номера и посчитала количество -> сконвертировали в черновик
документа -> провели, и остаток записался по этому адресу.

Разделение слоёв:
- сырые скан-события (InventoryScanEvent) хранятся для аудита и отмены;
- сгруппированные строки (InventoryCountingLine) идут в документ;
- сам скан и черновик сессии склад НЕ меняют; остаток пишется только при
  проведении документа (существующий receipts.post_receipt).

Режим — «первичный ввод ячейки»: проведение ДОБАВЛЯЕТ остаток. Сверочный
пересчёт (зафиксировать факт) — отдельный будущий слой; поэтому есть явное
предупреждение и защита от повторного проведения одной сессии.
"""
from decimal import Decimal

from django.conf import settings
from django.db import models


class InventoryCountingSession(models.Model):
    """Одна сессия пересчёта конкретной ячейки/места хранения."""

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        CONVERTED = "converted", "Черновик документа создан"
        POSTED = "posted", "Проведено"
        CANCELLED = "cancelled", "Отменено"

    storage_location = models.ForeignKey(
        "warehouse.StorageLocation", verbose_name="Место хранения",
        on_delete=models.PROTECT, related_name="counting_sessions",
    )
    full_address = models.CharField("Полный адрес", max_length=80)
    title = models.CharField("Название", max_length=120, blank=True)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.DRAFT
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    converted_receipt = models.ForeignKey(
        "receipts.Receipt", verbose_name="Документ инвентаризации",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="counting_session", editable=False,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто начал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    posted_at = models.DateTimeField("Проведено (когда)", null=True, blank=True)

    class Meta:
        verbose_name = "Пересчёт ячейки"
        verbose_name_plural = "Пересчёты ячеек"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"Инвентаризация {self.full_address}"

    @property
    def is_draft(self) -> bool:
        return self.status == self.Status.DRAFT

    def counters(self) -> dict:
        """Сводка по строкам и сканам для шапки/списка.

        Разделение понятий (hotfix 32.3): total_scans — сырые сканы;
        unique — позиции (уникальные номера); total_quantity — деталей
        (сумма количеств строк, ручная правка учитывается); total_value —
        стоимость ячейки (сумма количество * цена клиента, Decimal).
        Счётчик warehouse остаётся внутренним: карточка на складе — не то
        же самое, что деталь физически лежит в ячейке.
        """
        lines = self.lines.all()
        return {
            "total_scans": sum(line.scan_count for line in lines),
            "unique": len(lines),
            "total_quantity": sum(
                (line.quantity_counted for line in lines), Decimal("0")
            ),
            "total_value": sum(
                (
                    line.quantity_counted * line.final_customer_price_rub
                    for line in lines
                    if line.final_customer_price_rub is not None
                ),
                Decimal("0"),
            ),
            "warehouse": sum(1 for line in lines if line.source == "warehouse"),
            "brp": sum(1 for line in lines if line.source == "brp_catalog"),
            "unknown": sum(1 for line in lines if line.source == "unknown"),
        }


class InventoryScanEvent(models.Model):
    """Сырое событие скана (для аудита и отмены последнего)."""

    session = models.ForeignKey(
        InventoryCountingSession, on_delete=models.CASCADE, related_name="scans"
    )
    raw_value = models.CharField("Отсканировано", max_length=120)
    normalized_value = models.CharField(max_length=120, db_index=True)
    matched_line = models.ForeignKey(
        "counting.InventoryCountingLine", on_delete=models.SET_NULL,
        null=True, blank=True, related_name="scan_events",
    )
    is_reverted = models.BooleanField("Отменён", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
        null=True, blank=True, related_name="+",
    )

    class Meta:
        verbose_name = "Скан-событие"
        verbose_name_plural = "Скан-события"
        ordering = ["-id"]

    def __str__(self) -> str:
        return self.raw_value


class InventoryCountingLine(models.Model):
    """Сгруппированная строка: один нормализованный номер = одна строка."""

    class Source(models.TextChoices):
        WAREHOUSE = "warehouse", "На складе"
        BRP = "brp_catalog", "BRP-каталог"
        UNKNOWN = "unknown", "Неизвестно"
        MANUAL = "manual", "Вручную"

    session = models.ForeignKey(
        InventoryCountingSession, on_delete=models.CASCADE, related_name="lines"
    )
    scanned_value = models.CharField("Номер", max_length=120)
    normalized_value = models.CharField(max_length=120, db_index=True)
    warehouse_part = models.ForeignKey(
        "catalog.PartType", verbose_name="Карточка склада",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    brp_catalog_part = models.ForeignKey(
        "brp.BrpCatalogPart", verbose_name="Позиция BRP",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    display_name = models.CharField("Название", max_length=255, blank=True)
    source = models.CharField(
        "Источник", max_length=20, choices=Source.choices, default=Source.UNKNOWN
    )
    quantity_counted = models.DecimalField(
        "Количество", max_digits=12, decimal_places=3, default=0
    )
    scan_count = models.PositiveIntegerField("Сканов", default=0)
    final_customer_price_rub = models.DecimalField(
        "Цена клиента (₽)", max_digits=16, decimal_places=6, null=True, blank=True
    )
    comment = models.CharField("Комментарий", max_length=255, blank=True)
    last_scanned_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "Строка пересчёта"
        verbose_name_plural = "Строки пересчёта"
        ordering = ["-last_scanned_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["session", "normalized_value"],
                name="uniq_countingline_session_norm",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.scanned_value} x {self.quantity_counted}"

    @property
    def needs_review(self) -> bool:
        return self.source == self.Source.UNKNOWN
