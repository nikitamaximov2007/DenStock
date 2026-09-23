"""Bind the explicitly authorized DenisStock Telegram admin identity."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.customer_requests import operator_console
from apps.customer_requests.models import StaffMessengerBinding


class Command(BaseCommand):
    help = "Bind one explicitly authorized Telegram provider identity as NIKITA / ADMIN."

    def add_arguments(self, parser):
        parser.add_argument("--provider-user-id", type=int, required=True)
        parser.add_argument("--username", default="admin")
        parser.add_argument("--confirm", action="store_true")

    def handle(self, *args, **options):
        if not operator_console.enabled():
            raise CommandError("CUSTOMER_OPERATOR_CONSOLE_ENABLED=false")
        if not options["confirm"]:
            raise CommandError("pass --confirm to bind the explicitly authorized identity")
        provider_user_id = options["provider_user_id"]
        if provider_user_id <= 0:
            raise CommandError("provider-user-id must be positive")

        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=options["username"])
        except user_model.DoesNotExist:
            raise CommandError("internal user was not found") from None
        if not user.is_active or not user.can_manage_sales:
            raise CommandError("internal user is not active or lacks the sales permission")

        with transaction.atomic():
            identity = StaffMessengerBinding.objects.select_for_update().filter(
                provider=StaffMessengerBinding.Provider.TELEGRAM,
                provider_user_id=provider_user_id,
            ).first()
            if identity and (
                identity.user_id != user.pk or identity.operator_key != "NIKITA"
            ):
                raise CommandError("provider identity is already bound to another role")
            existing = StaffMessengerBinding.objects.select_for_update().filter(
                user=user,
                provider=StaffMessengerBinding.Provider.TELEGRAM,
                operator_key="NIKITA",
            ).first()
            if existing and identity and existing.pk != identity.pk:
                raise CommandError("NIKITA already has another Telegram identity")
            binding = identity or existing
            if binding is None:
                binding = StaffMessengerBinding.objects.create(
                    user=user,
                    operator_key="NIKITA",
                    provider=StaffMessengerBinding.Provider.TELEGRAM,
                    provider_user_id=provider_user_id,
                    customer_visible_label="NIKITA",
                    created_by=user,
                )
            else:
                binding.is_active = True
                binding.operator_mode = False
                binding.customer_visible_label = "NIKITA"
                binding.created_by = user
                binding.save(update_fields=[
                    "is_active", "operator_mode", "customer_visible_label", "created_by",
                    "updated_at",
                ])
            operator_console.clear_context(binding=binding)
            operator_console.clear_photo_context(binding=binding)

        self.stdout.write(
            self.style.SUCCESS(
                "NIKITA / ADMIN Telegram binding active "
                f"(identity tail …{str(provider_user_id)[-4:]})."
            )
        )
