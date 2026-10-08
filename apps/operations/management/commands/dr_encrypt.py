"""Stage the newest verified backup as an age bundle plus signed receipt.

Runs inside the web container (it holds the Ed25519 signing key).  Only the
age *public* recipient is configured here.  Nothing is uploaded: the host
uploader ``scripts/operations/dr_upload.py`` does that with rotation.
"""

import json
import os
import re
import shutil
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.operations import backup
from apps.operations.dr_archive import ArchiveError, encrypt_verified_run, signed_cipher_receipt
from apps.operations.dr_local import ENCRYPTED_NAME, RECEIPT_NAME, RUN_NAME
from apps.operations.emergency_manifest import read_manifest, validate_manifest


def newest_complete_run(root: Path) -> Path:
    for run in sorted((p for p in root.iterdir() if p.is_dir()), reverse=True):
        if not RUN_NAME.fullmatch(run.name) or not (run / "manifest.json").is_file():
            continue
        try:
            manifest = read_manifest(run / "manifest.json")
        except Exception:  # noqa: BLE001 - an unreadable run is simply not a candidate
            continue
        if manifest.get("private_media_filename") and validate_manifest(run).ok:
            return run
    raise CommandError("Нет полного проверенного бэкапа с private_media.")


class Command(BaseCommand):
    help = "Зашифровать (age) новейший полный бэкап и подписать receipt. Ничего не загружает."
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("--staging", required=True, help="Каталог для зашифрованных поколений")
        parser.add_argument(
            "--recipient", action="append", default=None,
            help="Публичный age recipient (можно несколько); иначе DENSTOCK_DR_AGE_RECIPIENTS",
        )

    def handle(self, *args, **options):
        recipients = options["recipient"] or [
            r for r in re.split(r"[,\s]+", os.environ.get("DENSTOCK_DR_AGE_RECIPIENTS", "")) if r
        ]
        if not recipients:
            raise CommandError("Не задан публичный age recipient.")
        staging = Path(options["staging"]).resolve()
        root = backup.backup_root().resolve()
        # prune_old_runs() and the backup UI treat every directory inside the
        # backup root as a run: a staging dir there would be pruned as "partial".
        if staging == root or root in staging.parents:
            raise CommandError("--staging должен лежать ВНЕ каталога бэкапов.")
        staging.mkdir(parents=True, exist_ok=True, mode=0o700)
        run = newest_complete_run(backup.backup_root())
        target = staging / run.name
        if (target / RECEIPT_NAME).is_file():
            self.stdout.write(f"Поколение уже подготовлено: {run.name}")
            return
        work = staging / f".work-{run.name}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True, mode=0o700)
        try:
            cipher = work / ENCRYPTED_NAME
            encrypt_verified_run(run, cipher, recipients)
            receipt = signed_cipher_receipt(run.name, cipher)
            (work / RECEIPT_NAME).write_text(json.dumps(receipt, sort_keys=True) + "\n")
            shutil.rmtree(target, ignore_errors=True)
            os.replace(work, target)
        except (ArchiveError, OSError, ValueError) as exc:
            shutil.rmtree(work, ignore_errors=True)
            raise CommandError(f"Подготовка DR-поколения не удалась: {exc}") from exc
        staged = sorted(p for p in staging.iterdir() if p.is_dir() and RUN_NAME.fullmatch(p.name))
        for old in staged[:-2]:  # staging holds ciphertext only; keep the newest two
            shutil.rmtree(old, ignore_errors=True)
        self.stdout.write(self.style.SUCCESS(f"DR-поколение готово: {run.name}"))
