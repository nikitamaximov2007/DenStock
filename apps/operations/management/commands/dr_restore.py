"""Restore an age bundle into a NEW database and NEW media directories (all-or-nothing)."""

import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.operations.dr_restore import RestoreError, restore_bundle


class Command(BaseCommand):
    help = (
        "Расшифровать age-архив и восстановить в НОВУЮ базу и НОВЫЕ каталоги. "
        "Ничего существующего не перезаписывается; при ошибке выполняется полный откат."
    )
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("bundle")
        parser.add_argument("--identity", required=True, help="Файл приватного age-ключа")
        parser.add_argument("--public-key", required=True, help="Закреплённый Ed25519 public key")
        parser.add_argument("--new-database", required=True)
        parser.add_argument("--media-target", required=True)
        parser.add_argument("--private-media-target", required=True)
        parser.add_argument("--work-dir", required=True)
        parser.add_argument("--pg-host", default="localhost")
        parser.add_argument("--pg-port", default="5432")
        parser.add_argument("--pg-user", required=True)
        parser.add_argument("--key-id", default="production-1")
        parser.add_argument("--yes", action="store_true")

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Требуется --yes. Пароль PostgreSQL берётся из PGPASSWORD.")
        try:
            report = restore_bundle(
                Path(options["bundle"]), Path(options["identity"]),
                Path(options["public_key"]),
                pg={"host": options["pg_host"], "port": options["pg_port"],
                    "user": options["pg_user"], "password": os.environ.get("PGPASSWORD", "")},
                new_database=options["new_database"],
                media_target=Path(options["media_target"]),
                private_target=Path(options["private_media_target"]),
                work_parent=Path(options["work_dir"]), key_id=options["key_id"],
            )
        except RestoreError as exc:
            raise CommandError(str(exc)) from exc
        for line in report.checks:
            self.stdout.write(f"OK: {line}")
        self.stdout.write(self.style.SUCCESS(
            f"Восстановлено в {report.database} за {report.seconds:.1f} с (синтетический RTO "
            "не равен production RTO)."
        ))
