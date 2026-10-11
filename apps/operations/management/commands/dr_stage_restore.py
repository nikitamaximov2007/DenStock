from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from apps.operations.offline_restore import (
    ExternalPrivateMedia,
    StageRestoreError,
    StageTargets,
    stage_restore,
)
from apps.operations.private_media import Inventory


class Command(BaseCommand):
    help = (
        "Offline DR: восстановить подписанный бэкап в НОВУЮ базу и НОВЫЕ каталоги "
        "media/private_media с журналом и проверкой. Рабочие база и тома не "
        "затрагиваются; переключения приложения нет."
    )

    def add_arguments(self, parser):
        parser.add_argument("run_dir", help="Каталог бэкапа (manifest.json, db.dump, ...)")
        parser.add_argument("--target-db", required=True, help="Имя новой staging-базы")
        parser.add_argument("--media-dir", required=True, help="Новый каталог для media")
        parser.add_argument(
            "--private-media-dir", required=True, help="Новый каталог для private_media"
        )
        parser.add_argument("--journal", required=True, help="Новый файл журнала (JSON lines)")
        parser.add_argument(
            "--private-media-archive",
            help="Отдельно снятый private_media .tar.gz (для бэкапов без private_media)",
        )
        parser.add_argument("--private-media-files", type=int)
        parser.add_argument("--private-media-bytes", type=int)
        parser.add_argument("--private-media-tree", help="SHA-256 дерева private_media")

    def handle(self, *args, **options):
        external = None
        inventory_args = (
            options["private_media_files"], options["private_media_bytes"],
            options["private_media_tree"],
        )
        if options["private_media_archive"]:
            if any(value is None for value in inventory_args):
                raise CommandError(
                    "Для внешнего архива private_media укажите --private-media-files, "
                    "--private-media-bytes и --private-media-tree."
                )
            external = ExternalPrivateMedia(
                Path(options["private_media_archive"]), Inventory(*inventory_args)
            )
        elif any(value is not None for value in inventory_args):
            raise CommandError("Инвентарь private_media задан без --private-media-archive.")
        targets = StageTargets(
            database=options["target_db"],
            media_dir=Path(options["media_dir"]),
            private_media_dir=Path(options["private_media_dir"]),
            journal=Path(options["journal"]),
        )
        try:
            result = stage_restore(options["run_dir"], targets, private_media=external)
        except StageRestoreError as exc:
            raise CommandError(f"{exc} (журнал: {targets.journal})") from exc
        files = result["proof"]["files"]
        self.stdout.write(
            f"Бэкап {result['manifest']['created_at']} восстановлен в staging "
            f"{targets.database}; файлов проверено {files['checked']}, "
            f"в публичном legacy {files['legacy_public']}, отсутствует {files['missing_count']}."
        )
        style = self.style.SUCCESS if result["verdict"] == "staged" else self.style.WARNING
        self.stdout.write(style(f"Итог: {result['verdict']}. Журнал: {targets.journal}"))
