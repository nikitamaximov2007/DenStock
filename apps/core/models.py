from django.conf import settings
from django.db import models

from .files import image_upload_to


class BaseImage(models.Model):
    """Слой 24 — абстрактная база для фотографий (без таблицы).

    Конкретные `PartTypeImage`/`PartItemImage` добавляют FK на владельца и задают
    `upload_folder`/`owner_id`/`siblings`. Это информационный слой: записи и файлы в
    media, но никакой складской физики (см. сервисы в `apps.core.images`).
    """

    upload_folder = "images"  # переопределяется наследником (part-types / part-items)

    image = models.FileField("Файл", upload_to=image_upload_to)
    caption = models.CharField("Подпись", max_length=255, blank=True)
    is_primary = models.BooleanField("Главное фото", default=False)
    sort_order = models.PositiveIntegerField("Порядок", default=0)
    is_active = models.BooleanField("Активно", default=True)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто загрузил",
        on_delete=models.SET_NULL, null=True, blank=True,
        related_name="%(app_label)s_%(class)s_uploads",  # обязателен для abstract
    )
    uploaded_at = models.DateTimeField("Загружено", auto_now_add=True)

    class Meta:
        abstract = True
        ordering = ["sort_order", "uploaded_at"]

    def __str__(self) -> str:
        return self.caption or f"{self._meta.verbose_name} #{self.pk}"

    @property
    def owner_id(self):  # noqa: A003 — используется в image_upload_to
        raise NotImplementedError

    @property
    def siblings(self):
        """Queryset изображений того же владельца (для primary-логики)."""
        raise NotImplementedError


class UnresolvedScan(models.Model):
    """Журнал нераспознанных сканов (Слой 11).

    Пишется ТОЛЬКО из endpoint при реальном unknown-резолве (чистый сервис
    `resolve_scan` журнал не трогает). Питает будущий виджет «неопознанные
    детали» на главной панели: `resolved`/`resolved_part` закрывают строку при
    разборе (создание/привязка карточки) — это отдельная задача дашборда.
    """

    raw_value = models.CharField("Код", max_length=255)
    normalized_value = models.CharField(max_length=255, blank=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто сканировал",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    context = models.CharField("Контекст", max_length=60, blank=True)
    note = models.CharField("Примечание", max_length=255, blank=True)
    resolved = models.BooleanField("Разобран", default=False)
    resolved_part = models.ForeignKey(
        "catalog.PartType", verbose_name="Привязан к детали",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "Нераспознанный скан"
        verbose_name_plural = "Нераспознанные сканы"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.raw_value

    def save(self, *args, **kwargs):
        if not self.normalized_value and self.raw_value:
            # Ленивый импорт: core грузится раньше catalog в INSTALLED_APPS.
            from apps.catalog.models import normalize_number

            self.normalized_value = normalize_number(self.raw_value)
        super().save(*args, **kwargs)
