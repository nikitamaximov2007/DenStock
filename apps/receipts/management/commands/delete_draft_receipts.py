"""Безопасное удаление ЧЕРНОВИКОВ поступлений (например, демо POS-000001/2).

    python manage.py delete_draft_receipts --receipt-id 1 --dry-run
    python manage.py delete_draft_receipts --receipt-id 1 --commit

Удаляются только черновики: они не имеют партии, лотов, движений и остатков,
поэтому удаление безопасно. Проведённые документы и документы, связанные с
пересчётом ячейки, команда удалять отказывается. Ничего не удаляется молча:
без --commit выполняется dry-run со списком строк.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.receipts.models import Receipt


class Command(BaseCommand):
    help = "Удалить черновик поступления по id (только draft, без склада)."

    def add_arguments(self, parser):
        parser.add_argument("--receipt-id", type=int, required=True,
                            help="ID черновика поступления")
        parser.add_argument("--commit", action="store_true",
                            help="Удалить (без флага: dry-run)")
        parser.add_argument("--dry-run", action="store_true",
                            help="Явно указать dry-run (поведение по умолчанию)")

    def handle(self, *args, **options):
        if options["commit"] and options["dry_run"]:
            raise CommandError("Выберите одно: --dry-run ИЛИ --commit.")
        receipt = Receipt.objects.filter(pk=options["receipt_id"]).first()
        if receipt is None:
            raise CommandError(f"Поступление {options['receipt_id']} не найдено.")
        if receipt.status != Receipt.Status.DRAFT:
            raise CommandError(
                f"{receipt.number} не черновик ({receipt.get_status_display()}): "
                "проведённые документы не удаляются."
            )
        if receipt.counting_session.exists():
            raise CommandError(
                f"{receipt.number} связан с пересчётом ячейки: удалять нельзя."
            )
        write = self.stdout.write
        mode = "УДАЛЕНО (--commit)" if options["commit"] else "DRY-RUN (ничего не удалено)"
        write(f"Режим: {mode}")
        write(f"Черновик: {receipt.number} (id={receipt.pk}), "
              f"поставщик: {receipt.supplier or 'не выбран'}")
        lines = list(receipt.lines.select_related("part_type", "location"))
        write(f"Строк: {len(lines)}")
        for line in lines:
            write(f"  {line.part_type} x {line.quantity} @ {line.location.code}")
        if not options["commit"]:
            return
        with transaction.atomic():
            receipt.lines.all().delete()
            receipt.delete()
        self.stdout.write(self.style.SUCCESS("Черновик удалён. Склад не менялся."))
