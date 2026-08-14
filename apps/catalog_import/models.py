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
        settings.AUTH_USER_MODEL, verbose_name="Кто загрузил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField("Загружен", auto_now_add=True)
    checked_at = models.DateTimeField("Проверен (когда)", null=True, blank=True)
    applied_at = models.DateTimeField("Применён (когда)", null=True, blank=True)
    applied_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто применил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
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
