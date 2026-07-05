"""Layer 30 — журнал аварийных восстановлений (RestoreJob).

Единственная модель эксплуатационного приложения. Важно: восстановление
перезаписывает саму базу, поэтому строка журнала пишется ПОСЛЕ операции
(в восстановленную и домигрированную базу при успехе; в текущую базу при
ошибке до restore). Надёжный след независимо от БД — файл
`<BACKUP_ROOT>/restore.log`. Секретов модель не хранит.
"""
from django.conf import settings
from django.db import models


class RestoreJob(models.Model):
    """Одна попытка восстановления из веб-интерфейса."""

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        VERIFYING = "verifying", "Проверка бэкапа"
        PRE_BACKUP = "pre_backup", "Pre-restore бэкап"
        RESTORING = "restoring", "Восстановление"
        MIGRATED = "migrated", "Миграции применены"
        COMPLETED = "completed", "Завершено"
        FAILED = "failed", "Ошибка"

    run_id = models.CharField("Бэкап (run id)", max_length=64)
    status = models.CharField(
        "Статус", max_length=20, choices=Status.choices, default=Status.PENDING
    )
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="Кто запустил",
        on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    started_by_username = models.CharField(
        "Логин запустившего", max_length=150, blank=True
    )  # дублируем текстом: FK может не пережить восстановление другой базы
    pre_restore_run_id = models.CharField(
        "Pre-restore бэкап", max_length=64, blank=True
    )
    log = models.TextField("Журнал шагов", blank=True)
    error = models.TextField("Ошибка", blank=True)
    created_at = models.DateTimeField("Запущено", auto_now_add=True)
    finished_at = models.DateTimeField("Завершено", null=True, blank=True)

    class Meta:
        verbose_name = "Восстановление из бэкапа"
        verbose_name_plural = "Восстановления из бэкапов"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.run_id} ({self.get_status_display()})"
