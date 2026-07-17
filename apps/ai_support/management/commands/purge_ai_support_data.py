from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.ai_support.files import delete_private_file
from apps.ai_support.models import DeveloperTicket, SupportAttachment, SupportConversation


class Command(BaseCommand):
    help = "Dry-run или подтверждённая очистка просроченных данных ИИ-поддержки."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Фактически удалить найденные записи и приватные файлы.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        attachment_cutoff = now - timedelta(
            days=settings.AI_SUPPORT_ATTACHMENT_RETENTION_DAYS
        )
        conversation_cutoff = now - timedelta(
            days=settings.AI_SUPPORT_CONVERSATION_RETENTION_DAYS
        )
        expired_conversations = SupportConversation.objects.filter(
            updated_at__lt=conversation_cutoff
        )
        expired_tickets = DeveloperTicket.objects.filter(updated_at__lt=conversation_cutoff)
        expired_attachments = SupportAttachment.objects.filter(
            created_at__lt=attachment_cutoff
        )
        conversation_attachment_ids = SupportAttachment.objects.filter(
            message__conversation__in=expired_conversations
        ).values_list("pk", flat=True)
        attachments = SupportAttachment.objects.filter(
            pk__in=set(expired_attachments.values_list("pk", flat=True)).union(
                conversation_attachment_ids
            )
        )
        paths = list(attachments.values_list("relative_path", flat=True))
        self.stdout.write(
            "AI SUPPORT PURGE PLAN: "
            f"attachments={len(paths)}, conversations={expired_conversations.count()}, "
            f"tickets={expired_tickets.count()}, files={len(paths)}"
        )
        if not options["confirm"]:
            self.stdout.write("DRY RUN: nothing deleted. Use --confirm to delete.")
            return
        try:
            for relative_path in paths:
                delete_private_file(relative_path)
        except OSError as exc:
            raise CommandError("Не удалось удалить приватный файл; DB не изменена.") from exc
        with transaction.atomic():
            attachments.delete()
            expired_tickets.delete()
            expired_conversations.delete()
        self.stdout.write(self.style.SUCCESS("AI SUPPORT PURGE COMPLETED"))
