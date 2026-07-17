from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from apps.ai_support.runtime import delete_request_directory, stale_request_directories


class Command(BaseCommand):
    help = "Dry-run или подтверждённая очистка старых request-каталогов ИИ-поддержки."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Фактически удалить только проверенные старые request-каталоги.",
        )
        parser.add_argument(
            "--older-than-hours",
            type=int,
            default=settings.AI_SUPPORT_CODEX_RUNTIME_RETENTION_HOURS,
            help="Минимальный возраст request-каталога в часах.",
        )

    def handle(self, *args, **options):
        workspace = settings.AI_SUPPORT_CODEX_WORKSPACE
        try:
            candidates = stale_request_directories(
                workspace,
                older_than_hours=options["older_than_hours"],
            )
        except (OSError, ValueError) as exc:
            raise CommandError("Codex runtime workspace небезопасен или недоступен.") from exc
        self.stdout.write(f"AI SUPPORT RUNTIME PURGE PLAN: directories={len(candidates)}")
        for candidate in candidates:
            self.stdout.write(candidate.name)
        if not options["confirm"]:
            self.stdout.write("DRY RUN: nothing deleted. Use --confirm to delete.")
            return
        for candidate in candidates:
            try:
                delete_request_directory(workspace, candidate)
            except (OSError, ValueError) as exc:
                raise CommandError(
                    "Runtime directory changed during cleanup; deletion stopped."
                ) from exc
        self.stdout.write(self.style.SUCCESS("AI SUPPORT RUNTIME PURGE COMPLETED"))
