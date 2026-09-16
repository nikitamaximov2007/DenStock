"""Operational state and audit models.

Единственная модель эксплуатационного приложения. Важно: восстановление
перезаписывает саму базу, поэтому строка журнала пишется ПОСЛЕ операции
(в восстановленную и домигрированную базу при успехе; в текущую базу при
ошибке до restore). Надёжный след независимо от БД — файл
`<BACKUP_ROOT>/restore.log`. Секретов модель не хранит.
"""
import uuid

from django.conf import settings
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models import Q


class DeploymentState(models.Model):
    """Singleton lineage marker and global write state for this database."""

    SINGLETON_PK = 1

    class WriteState(models.TextChoices):
        NORMAL = "normal", "Обычная работа"
        MAINTENANCE = "maintenance", "Production только для чтения"
        EMERGENCY_ACTIVE = "emergency_active", "Автономная работа"
        EMERGENCY_FROZEN = "emergency_frozen", "Автономная копия заморожена"

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_PK, editable=False)
    database_identity = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    business_generation = models.PositiveBigIntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text="Консервативный счётчик принятых write-запросов.",
    )
    write_state = models.CharField(
        max_length=24,
        choices=WriteState.choices,
        default=WriteState.NORMAL,
        db_index=True,
    )
    state_reason = models.CharField(max_length=255, blank=True)
    authorized_emergency_primary_id = models.UUIDField(null=True, blank=True)
    primary_authorization_epoch = models.PositiveBigIntegerField(default=0)
    state_changed_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Состояние deployment"
        verbose_name_plural = "Состояние deployment"

    def __str__(self):
        return f"{self.database_identity} ({self.get_write_state_display()})"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        return super().save(*args, **kwargs)

    @classmethod
    def get_solo(cls, *, using="default"):
        state, _ = cls.objects.using(using).get_or_create(pk=cls.SINGLETON_PK)
        return state


class OfflineSession(models.Model):
    """Durable controlled-failover lifecycle stored with the local database."""

    class Kind(models.TextChoices):
        PLANNED = "planned", "Плановый переход"
        UNPLANNED = "unplanned", "Внезапная потеря связи"

    class Status(models.TextChoices):
        ACTIVE = "active", "Автономная работа"
        FREEZING = "freezing", "Остановка записи"
        EXPORT_FAILED = "export_failed", "Ошибка экспорта"
        FROZEN = "frozen", "Экспорт завершён"
        ELIGIBLE = "eligible", "Допустим controlled failback"
        CONFLICT = "conflict", "Обнаружен конфликт"
        BLOCKED = "blocked", "Возврат заблокирован"
        COMPLETED = "completed", "Возврат подтверждён"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    kind = models.CharField(max_length=12, choices=Kind.choices)
    status = models.CharField(max_length=20, choices=Status.choices, db_index=True)
    local_hostname = models.CharField(max_length=255)
    instance_id = models.CharField(max_length=128)
    base_backup_run_id = models.CharField(max_length=128)
    base_backup_created_at = models.DateTimeField()
    base_manifest = models.JSONField(default=dict, editable=False)
    base_data_marker = models.JSONField(default=dict, editable=False)
    base_media_sha256 = models.CharField(max_length=64, blank=True)
    base_app_commit = models.CharField(max_length=64, blank=True)
    base_migration_fingerprint = models.CharField(max_length=64)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offline_sessions_started",
    )
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    final_backup_run_id = models.CharField(max_length=128, blank=True)
    final_manifest = models.JSONField(default=dict, editable=False)
    final_data_marker = models.JSONField(default=dict, editable=False)
    failback_report = models.JSONField(default=dict, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["instance_id"],
                condition=Q(status__in=["active", "freezing", "export_failed"]),
                name="uniq_unfinished_offline_session",
            )
        ]

    def __str__(self):
        return f"{self.id} ({self.get_status_display()})"


class EmergencyAuditEvent(models.Model):
    """Credential-free append-only lifecycle audit."""

    event_type = models.CharField(max_length=64, db_index=True)
    outcome = models.CharField(max_length=16, db_index=True)
    session = models.ForeignKey(
        OfflineSession,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_events",
    )
    actor = models.CharField(max_length=150, blank=True)
    details = models.JSONField(default=dict, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.event_type}: {self.outcome}"


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


class TelegramBotRuntime(models.Model):
    """Heartbeat, update offset and single-consumer lease of the request bot.

    Operational state, deliberately outside the business apps: the heartbeat
    moves every few seconds and must neither bump the business generation nor
    change the backup fingerprint. Business rows (messages, deliveries) live in
    ``customer_requests`` and stay guarded and fingerprinted.
    """

    SINGLETON_PK = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_PK, editable=False)
    last_update_id = models.BigIntegerField("Последнее обработанное обновление", default=0)
    worker_id = models.CharField("Экземпляр бота", max_length=64, blank=True)
    lease_expires_at = models.DateTimeField("Аренда до", null=True, blank=True)
    started_at = models.DateTimeField("Запущен", null=True, blank=True)
    heartbeat_at = models.DateTimeField("Последний сигнал", null=True, blank=True)
    last_update_at = models.DateTimeField("Последнее обновление Telegram", null=True, blank=True)
    bot_username = models.CharField("Имя бота", max_length=64, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    last_error_at = models.DateTimeField("Время ошибки", null=True, blank=True)

    class Meta:
        verbose_name = "Состояние Telegram-бота"
        verbose_name_plural = "Состояние Telegram-бота"

    def __str__(self) -> str:
        return f"Telegram-бот {self.bot_username or ''}".strip()


class MaxBotRuntime(models.Model):
    """Heartbeat and single-worker lease of the MAX request bot.

    Operational state next to ``TelegramBotRuntime`` for the same reason: the
    heartbeat moves constantly and must not count as a business change.
    """

    SINGLETON_PK = 1

    id = models.PositiveSmallIntegerField(primary_key=True, default=SINGLETON_PK, editable=False)
    worker_id = models.CharField("Экземпляр бота", max_length=64, blank=True)
    lease_expires_at = models.DateTimeField("Аренда до", null=True, blank=True)
    started_at = models.DateTimeField("Запущен", null=True, blank=True)
    heartbeat_at = models.DateTimeField("Последний сигнал", null=True, blank=True)
    # Requests sent before MAX first went live were never promised a MAX
    # conversation; only later ones are announced to operators.
    announce_requests_since = models.DateTimeField(
        "Объявлять заявки начиная с", null=True, blank=True
    )
    bot_username = models.CharField("Имя бота", max_length=64, blank=True)
    last_error = models.CharField("Последняя ошибка", max_length=255, blank=True)
    last_error_at = models.DateTimeField("Время ошибки", null=True, blank=True)

    class Meta:
        verbose_name = "Состояние MAX-бота"
        verbose_name_plural = "Состояние MAX-бота"

    def __str__(self) -> str:
        return f"MAX-бот {self.bot_username or ''}".strip()
