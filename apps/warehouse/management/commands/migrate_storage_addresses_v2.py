import json

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from apps.warehouse.address_migration import (
    StorageAddressMigrationError,
    apply_storage_address_v2_plan,
    build_storage_address_v2_plan,
    load_address_mapping,
)
from apps.warehouse.services import StorageLocationRenameError


class Command(BaseCommand):
    help = "Dry-run or explicitly apply a collision-safe legacy S-L-D to S-D mapping."

    def add_arguments(self, parser):
        parser.add_argument("mapping_file")
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--confirm", default="")
        parser.add_argument("--expected-fingerprint", default="")
        parser.add_argument("--user-id", type=int)

    def handle(self, *args, **options):
        try:
            mapping = load_address_mapping(options["mapping_file"])
            plan = build_storage_address_v2_plan(mapping)
        except StorageAddressMigrationError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(plan.as_dict(), ensure_ascii=False, indent=2))
        if not options["apply"]:
            self.stdout.write(self.style.WARNING("DRY-RUN: база данных не изменена."))
            return
        if options["confirm"] != "APPLY-STORAGE-ADDRESS-V2":
            raise CommandError("Для apply укажите --confirm APPLY-STORAGE-ADDRESS-V2.")
        if not options["expected_fingerprint"]:
            raise CommandError("Для apply укажите fingerprint предыдущего dry-run.")
        actor = None
        if options["user_id"]:
            actor = get_user_model().objects.filter(pk=options["user_id"]).first()
            if actor is None:
                raise CommandError("Пользователь --user-id не найден.")
        try:
            result = apply_storage_address_v2_plan(
                mapping,
                expected_fingerprint=options["expected_fingerprint"],
                by=actor,
            )
        except (StorageAddressMigrationError, StorageLocationRenameError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=2))
        self.stdout.write(self.style.SUCCESS("Storage Address V2 mapping применён."))
