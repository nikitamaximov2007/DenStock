"""Copy legacy messenger attachments to the private worker-readable volume."""

from pathlib import Path

from django.core.files.base import ContentFile
from django.core.management.base import BaseCommand

from apps.customer_requests.models import MaxMessage, TelegramMessage


class Command(BaseCommand):
    help = "Audit or copy messenger attachments from legacy media to private storage."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Copy missing private files.")
        parser.add_argument(
            "--remove-legacy",
            action="store_true",
            help="Delete legacy copies only after a private copy is verified.",
        )

    def handle(self, *args, **options):
        rows = [
            *TelegramMessage.objects.exclude(attachment="").order_by("pk"),
            *MaxMessage.objects.exclude(attachment="").order_by("pk"),
        ]
        storage = TelegramMessage._meta.get_field("attachment").storage
        private_root = Path(storage.location)
        copied = removed = missing = 0
        for row in rows:
            name = row.attachment.name
            private_exists = (private_root / name).is_file()
            legacy_exists = storage.legacy.exists(name)
            if private_exists:
                if options["remove_legacy"] and legacy_exists and options["apply"]:
                    storage.legacy.delete(name)
                    removed += 1
                continue
            if not legacy_exists:
                missing += 1
                continue
            if not options["apply"]:
                continue
            with storage.legacy.open(name, "rb") as source:
                private_name = storage.save(name, ContentFile(source.read()))
            if not (private_root / private_name).is_file():
                raise RuntimeError("Private attachment copy verification failed.")
            copied += 1
            if options["remove_legacy"]:
                storage.legacy.delete(name)
                removed += 1
        self.stdout.write(
            f"Всего вложений: {len(rows)}; скопировано: {copied}; "
            f"удалено legacy-копий: {removed}; отсутствует: {missing}."
        )
        if not options["apply"]:
            self.stdout.write("Режим проверки: изменений нет. Для применения добавьте --apply.")
