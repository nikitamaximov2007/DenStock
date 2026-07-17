import uuid

from django.conf import settings
from django.db import models
from django.db.models import Q


class SupportConversation(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "Активен"
        CLOSED = "closed", "Закрыт"
        ESCALATED = "escalated", "Создано обращение"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ai_support_conversations",
    )
    title = models.CharField("Тема", max_length=160, blank=True)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.ACTIVE)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["owner", "-updated_at"], name="ai_conv_owner_updated")]

    def __str__(self):
        return self.title or f"Разговор {self.pk}"


class SupportMessage(models.Model):
    class Role(models.TextChoices):
        USER = "user", "Пользователь"
        ASSISTANT = "assistant", "Ассистент"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        PROCESSING = "processing", "Обрабатывается"
        COMPLETED = "completed", "Завершено"
        FAILED = "failed", "Ошибка"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        SupportConversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=12, choices=Role.choices)
    text = models.TextField("Текст")
    sequence = models.PositiveIntegerField()
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)
    provider = models.CharField(max_length=40, blank=True)
    model = models.CharField(max_length=120, blank=True)
    latency_ms = models.PositiveIntegerField(null=True, blank=True)
    usage = models.JSONField(default=dict, blank=True)
    error_code = models.CharField(max_length=64, blank=True)
    idempotency_token = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="ai_message_conversation_sequence"
            ),
            models.UniqueConstraint(
                fields=["conversation", "idempotency_token"],
                condition=Q(idempotency_token__isnull=False),
                name="ai_message_conversation_token",
            ),
            models.CheckConstraint(condition=~Q(text=""), name="ai_message_text_not_empty"),
        ]

    def __str__(self):
        return f"{self.get_role_display()} #{self.sequence}"


class SupportAttachment(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    message = models.OneToOneField(
        SupportMessage, on_delete=models.CASCADE, related_name="attachment"
    )
    relative_path = models.CharField(max_length=500, unique=True)
    sha256 = models.CharField(max_length=64)
    size = models.PositiveBigIntegerField()
    mime_type = models.CharField(max_length=32)
    width = models.PositiveIntegerField()
    height = models.PositiveIntegerField()
    shared_with_provider_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"Вложение {self.pk}"


class SupportRating(models.Model):
    class Value(models.TextChoices):
        HELPFUL = "helpful", "Помог"
        UNHELPFUL = "unhelpful", "Не помог"

    assistant_message = models.OneToOneField(
        SupportMessage, on_delete=models.CASCADE, related_name="rating"
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="+")
    value = models.CharField(max_length=12, choices=Value.choices)
    comment = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.get_value_display()


class DeveloperTicket(models.Model):
    class Status(models.TextChoices):
        NEW = "new", "Новое"
        IN_PROGRESS = "in_progress", "В работе"
        RESOLVED = "resolved", "Решено"
        CLOSED = "closed", "Закрыто"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    conversation = models.ForeignKey(
        SupportConversation, on_delete=models.CASCADE, related_name="tickets"
    )
    author = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="ai_support_tickets"
    )
    attachment = models.ForeignKey(
        SupportAttachment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="tickets",
    )
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.NEW)
    description = models.TextField()
    conversation_snapshot = models.JSONField(default=list)
    diagnostic_snapshot = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"], name="ai_ticket_status_created")]

    def __str__(self):
        return f"Обращение {self.pk}"


class SupportUsageDay(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_support_usage_days"
    )
    date = models.DateField()
    request_count = models.PositiveIntegerField(default=0)
    input_tokens = models.PositiveBigIntegerField(default=0)
    output_tokens = models.PositiveBigIntegerField(default=0)
    active_request_token = models.UUIDField(null=True, blank=True)
    active_started_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "date"], name="ai_usage_user_date")
        ]

    def __str__(self):
        return f"{self.user_id}: {self.date}"


class SupportRuntimeGate(models.Model):
    """Singleton row used to serialize global provider capacity claims."""

    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return "AI support runtime gate"
