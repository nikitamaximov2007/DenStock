"""Merge exactly one explicit Customer pair. Dry-run by default."""

from django.core.management.base import BaseCommand, CommandError

from apps.customers.merge import (
    CustomerMergeError,
    execute_customer_merge,
    preview_customer_merge,
)
from apps.customers.models import Customer


class Command(BaseCommand):
    help = (
        "Merge one source Customer into one target Customer: reassigns every "
        "Sale/Reservation/RepairOrder/CustomerRequest/OrderedPart/account-link/"
        "payment-acknowledgement, tombstones the source (never deletes it), "
        "and writes a CustomerMergeReceipt. Dry-run by default - only --apply "
        "writes anything. Never iterates duplicate groups on its own: one "
        "explicit, reviewed pair per invocation."
    )

    def add_arguments(self, parser):
        parser.add_argument("--target", type=int, required=True, help="ID канонической карточки.")
        parser.add_argument("--source", type=int, required=True, help="ID карточки-источника.")
        parser.add_argument(
            "--apply", action="store_true", help="Выполнить слияние (по умолчанию dry-run)."
        )
        parser.add_argument("--reason", default="", help="Причина/комментарий для квитанции.")

    def handle(self, *args, **options):
        try:
            target = Customer.objects.get(pk=options["target"])
        except Customer.DoesNotExist as exc:
            raise CommandError(f"Целевая карточка #{options['target']} не найдена.") from exc
        try:
            source = Customer.objects.get(pk=options["source"])
        except Customer.DoesNotExist as exc:
            raise CommandError(f"Карточка-источник #{options['source']} не найдена.") from exc

        write = self.stdout.write
        mode = "APPLY" if options["apply"] else "DRY RUN"
        write(f"РЕЖИМ: {mode}")
        write(f"Источник: #{source.pk} {source.name} ({source.phone or 'без телефона'})")
        write(f"Цель:     #{target.pk} {target.name} ({target.phone or 'без телефона'})")

        try:
            if not options["apply"]:
                plan = preview_customer_merge(target, source)
                if plan.already_merged_here:
                    write("Источник уже объединён именно с этой целью - применять нечего.")
                    return
                write("Будет перенесено:")
                for key, count in plan.moved_counts.items():
                    write(f"  {key}: {count}")
                write(f"Итого: {plan.total_moved}")
                write("Изменений не внесено (dry-run). Повторите с --apply для применения.")
                return

            receipt = execute_customer_merge(
                target_id=target.pk, source_id=source.pk,
                by=None, reason=options["reason"],
            )
        except CustomerMergeError as exc:
            raise CommandError(str(exc)) from exc

        write(f"Готово. Квитанция #{receipt.pk} от {receipt.created_at:%Y-%m-%d %H:%M}.")
        for key, count in receipt.moved_counts.items():
            write(f"  {key}: {count}")
        write(f"Итого перенесено: {sum(receipt.moved_counts.values())}")
