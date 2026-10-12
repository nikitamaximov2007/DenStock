"""Move legacy messenger attachments out of the public media tree.

Attachments written before the private volume existed live under
``MEDIA_ROOT/customer_requests/``, which the proxy can serve anonymously.
``PrivateAttachmentStorage`` reads the private copy first and falls back to
that legacy copy, so a private copy under the SAME name takes over without
any database change.

    python manage.py reconcile_message_attachments_private
        read-only audit (default): what is public, private, missing, conflicting
    python manage.py reconcile_message_attachments_private --apply
        copy each public-only file to the same name in PRIVATE_MEDIA_ROOT
        (temporary file, fsync, no-overwrite link), then compare SHA-256
    python manage.py reconcile_message_attachments_private --apply --remove-legacy
        additionally delete a public copy, only when a byte-identical private
        copy exists

The database is never changed. A private file that differs from the public one
is a conflict: nothing is overwritten or removed and the command fails.
Output names rows by model and primary key, never by customer file name.

Earlier versions saved the copy through ``storage.save()``. Because the
storage reports legacy files as existing, Django picked an alternative name
(``<name>_XXXXXXX.<ext>``) that no row referenced, and ``--remove-legacy``
then deleted the only copy the row could open. Such alternative copies are
counted here as probable duplicates and are never deleted automatically.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.customer_requests.models import MaxMessage, TelegramMessage

ALTERNATIVE_SUFFIX = re.compile(r"_[A-Za-z0-9]{7}$")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, name: str) -> Path | None:
    root = root.resolve()
    candidate = (root / name).resolve()
    if root not in candidate.parents:
        return None
    return candidate


def _copy_without_overwrite(source: Path, target: Path, mode: int) -> None:
    """Publish ``target`` complete or not at all; never replace an existing file."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".reconcile-", suffix=".partial", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output, source.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, mode)
        os.link(temporary, target)  # FileExistsError instead of overwriting
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _alternative_copies(private: Path, legacy_hash: str) -> int:
    stem, suffix = private.name, ""
    if "." in private.name:
        stem, suffix = private.name.split(".", 1)
        suffix = "." + suffix
    count = 0
    if not private.parent.is_dir():
        return 0
    for candidate in private.parent.iterdir():
        name = candidate.name
        if not (candidate.is_file() and name.startswith(stem) and name.endswith(suffix)):
            continue
        middle = name[len(stem):len(name) - len(suffix)] if suffix else name[len(stem):]
        if ALTERNATIVE_SUFFIX.fullmatch(middle) and _sha256(candidate) == legacy_hash:
            count += 1
    return count


@dataclass
class Report:
    rows: int = 0
    private_only: int = 0
    public_only: int = 0
    identical_both: int = 0
    conflicts: list[str] = field(default_factory=list)
    missing: int = 0
    unsafe: list[str] = field(default_factory=list)
    copied: int = 0
    removed: int = 0
    duplicates: int = 0


class Command(BaseCommand):
    help = "Аудит или перенос вложений сообщений из публичного media в приватный том."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Скопировать публичные вложения в приватный том под тем же именем.",
        )
        parser.add_argument(
            "--remove-legacy",
            action="store_true",
            help="Только с --apply: удалить публичную копию после побайтовой сверки.",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        remove_legacy = options["remove_legacy"] and apply
        if options["remove_legacy"] and not apply:
            self.stdout.write(
                "--remove-legacy действует только вместе с --apply; ничего не удалено."
            )
        report = Report()
        for model in (TelegramMessage, MaxMessage):
            storage = model._meta.get_field("attachment").storage
            private_root = Path(storage.location)
            legacy_root = Path(storage.legacy.location)
            mode = storage.file_permissions_mode
            mode = 0o644 if mode is None else mode
            for pk, name in (
                model.objects.exclude(attachment="").order_by("pk").values_list("pk", "attachment")
            ):
                report.rows += 1
                label = f"{model.__name__}#{pk}"
                self._reconcile(
                    report, label, name, private_root, legacy_root, mode,
                    apply=apply, remove_legacy=remove_legacy,
                )

        self.stdout.write(
            f"Всего вложений: {report.rows}; только приватные: {report.private_only}; "
            f"только в публичном media: {report.public_only}; "
            f"в обоих местах и совпадают: {report.identical_both}; "
            f"конфликтов: {len(report.conflicts)}; отсутствует: {report.missing}; "
            f"скопировано: {report.copied}; удалено публичных копий: {report.removed}; "
            f"вероятных дублей прошлых запусков: {report.duplicates}."
        )
        if not apply:
            self.stdout.write("Режим проверки: изменений нет. Для копирования добавьте --apply.")
        if report.unsafe:
            raise CommandError("Небезопасные имена файлов: " + ", ".join(report.unsafe))
        if report.conflicts:
            raise CommandError(
                "Приватная и публичная копии различаются, ничего не изменено: "
                + ", ".join(report.conflicts)
            )

    def _reconcile(self, report, label, name, private_root, legacy_root, mode, *,
                   apply, remove_legacy):
        private = _inside(private_root, name)
        legacy = _inside(legacy_root, name)
        if private is None or legacy is None:
            report.unsafe.append(label)
            return
        private_exists = private.is_file()
        legacy_exists = legacy.is_file()
        if not private_exists and not legacy_exists:
            report.missing += 1
            return
        if not legacy_exists:
            report.private_only += 1
            return

        legacy_hash = _sha256(legacy)
        report.duplicates += _alternative_copies(private, legacy_hash)
        if private_exists:
            if _sha256(private) != legacy_hash:
                report.conflicts.append(label)
                return
            report.identical_both += 1
        else:
            report.public_only += 1
            if not apply:
                return
            _copy_without_overwrite(legacy, private, mode)
            if _sha256(private) != legacy_hash:
                private.unlink()
                raise CommandError(f"{label}: приватная копия не совпала с исходной, удалена.")
            report.copied += 1
            self.stdout.write(f"{label}: скопировано в приватный том, SHA-256 совпадает.")

        if remove_legacy:
            if _sha256(private) != _sha256(legacy):
                report.conflicts.append(label)
                return
            legacy.unlink()
            report.removed += 1
            self.stdout.write(f"{label}: публичная копия удалена после сверки.")
