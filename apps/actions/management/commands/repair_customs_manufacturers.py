"""Safe repair of stale "BRP" left by the old PartCustomsInfo.manufacturer default.

    python manage.py repair_customs_manufacturers                        # dry-run
    python manage.py repair_customs_manufacturers --apply                # Tier 1 only
    python manage.py repair_customs_manufacturers --apply --clear-unproven-brp

Dry-run is the default: nothing is written unless ``--apply`` is passed.

This command touches ONLY the live ``PartCustomsInfo.manufacturer`` field of
rows currently declared "BRP". It NEVER rewrites ``PartCustomsDataVersion``
(immutable history - what a historical Sale/Repair actually declared stays
exactly as declared) or ``CustomsOrderLine`` (frozen, already-sent customs
declarations). Saving the corrected live card creates a NEW
PartCustomsDataVersion, effective from now, through the existing
apps.actions.signals post_save hook - the same mechanism a real operator
edit already uses; no past version is touched or renumbered. Stock, Sale,
Repair, and prices are never read or written by this command.

Why only "BRP" is ever touched: the customs form has never let an operator
type a manufacturer by hand (see actions_customs_edit / system_customs_facts)
- every value that isn't "BRP" could only get there through a proven catalog
link or an explicitly chosen PartType.manufacturer, already resolved at save
time. "BRP" is the one value the old model default could write with zero
evidence, so it is the one value this command re-proves.

Two tiers, matched to how confident the evidence is (see
apps.actions.services.authoritative_manufacturer for the shared resolver):

    Tier 1 (--apply, on by default): manufacturer="BRP" is currently unproven
    AND a DIFFERENT brand IS proven right now (a BRP/Polaris/aftermarket
    catalog link, or an explicit PartType.manufacturer). This relabels the
    live card to that proven brand - a positive correction backed by
    evidence, not a guess. Example: AT-08776 declared "BRP", but
    PartType.manufacturer says "BRONCO" -> rewritten to "BRONCO".

    Tier 2 (--apply --clear-unproven-brp, off by default): manufacturer="BRP"
    is unproven and NOTHING is proven either way - a genuinely ambiguous
    manual row. Clearing it to "" (unknown) is very likely correct (nothing
    else could have produced "BRP" here but the old default), but the task
    explicitly wants a human gate before this destructive step even so, so
    it never runs as part of a plain --apply.

Idempotent: a row this command has already corrected reports the new value
as "BRP"-declared only if it genuinely still resolves to BRP, so a second
run touches zero further rows (see the second-run test).

Even without ever running --apply, History/Excel/the customs order queue
already treat an unproven "BRP" as excluded/unknown at READ time (see
authoritative_manufacturer and its call sites) - this command only cleans up
the stored value so the next real card edit does not re-freeze a version
still declaring the same stale default under an unrelated field change.
"""
import json

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.actions.management.commands.audit_customs_manufacturer_classification import (
    classify_part,
)
from apps.actions.models import PartCustomsInfo
from apps.catalog.models import PartType


class Command(BaseCommand):
    help = (
        "Repair PartCustomsInfo rows still declaring an unproven stale 'BRP' "
        "default. Dry-run unless --apply is passed; never touches historical "
        "PartCustomsDataVersion/CustomsOrderLine rows, stock, Sale/Repair, or prices."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply", action="store_true",
            help="Write Tier 1 (proven-different-brand) corrections. Without this, dry-run only.",
        )
        parser.add_argument(
            "--clear-unproven-brp", action="store_true", dest="clear_unproven",
            help=(
                "With --apply, also clear Tier 2 rows (declared BRP, nothing proven at all) "
                "to unknown. Off by default - the task requires an explicit owner decision "
                "for this destructive step even though it is very likely correct."
            ),
        )
        parser.add_argument(
            "--json", action="store_true", dest="as_json", help="Вывести итоги как JSON.",
        )
        parser.add_argument(
            "--list", type=int, default=50, dest="list_limit",
            help="Сколько строк на категорию показывать (0 - не показывать).",
        )

    def handle(self, *args, **options):
        apply = options["apply"]
        clear_unproven = options["clear_unproven"]
        parts = (
            PartType.objects.select_related("category", "manufacturer", "customs_info")
            .prefetch_related("numbers")
            .order_by("pk")
        )
        rows = [row for row in (classify_part(part) for part in parts) if row["has_customs_info"]]

        tier1 = [row for row in rows if row["stale_brp_high_confidence"]]
        tier2 = [row for row in rows if row["stale_brp_ambiguous"]]

        applied_tier1 = self._apply(tier1, target=lambda row: row["resolved_manufacturer"]) \
            if apply else []
        applied_tier2 = self._apply(tier2, target=lambda row: "") \
            if apply and clear_unproven else []

        payload = {
            "dry_run": not apply,
            "clear_unproven_requested": clear_unproven,
            "customs_info_rows_total": len(rows),
            "tier1_candidates": len(tier1),
            "tier1_applied": len(applied_tier1),
            "tier2_ambiguous_candidates": len(tier2),
            "tier2_applied": len(applied_tier2),
        }
        if options["as_json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            for key, value in payload.items():
                self.stdout.write(f"{key}: {value}")

        limit = options["list_limit"]
        if limit:
            self._list_section(
                "Tier 1 - declared BRP, proven другое (relabel on --apply)",
                tier1, applied_tier1, limit,
            )
            self._list_section(
                "Tier 2 - declared BRP, ничего не доказано (нужен --clear-unproven-brp)",
                tier2, applied_tier2, limit,
            )

        if not apply:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                "Dry-run: ничего не записано. Повторите с --apply для Tier 1."
            ))
        elif not clear_unproven and tier2:
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(
                f"{len(tier2)} строк Tier 2 оставлены как есть (нужен ручной пересмотр "
                "владельцем и --clear-unproven-brp, если решение подтверждено)."
            ))

    def _apply(self, candidates, *, target):
        if not candidates:
            return []
        applied = []
        with transaction.atomic():
            for row in candidates:
                part = row["part"]
                new_value = target(row)
                info = PartCustomsInfo.objects.select_for_update().get(part_type=part)
                # Re-check under the lock: a concurrent real edit may already
                # have moved this row off "BRP" since classify_part ran.
                if info.manufacturer.strip().upper() != row["declared_manufacturer"]:
                    continue
                info.manufacturer = new_value
                info.save(update_fields=["manufacturer", "updated_at"])
                applied.append(row)
        return applied

    def _list_section(self, title, candidates, applied, limit):
        if not candidates:
            return
        applied_ids = {row["part"].pk for row in applied}
        self.stdout.write("")
        self.stdout.write(f"{title} ({len(candidates)}):")
        for row in candidates[:limit]:
            part = row["part"]
            article = part.numbers.all()[0].value if part.numbers.all() else "-"
            mark = "applied" if part.pk in applied_ids else "dry-run"
            self.stdout.write(
                f"  PartType #{part.pk} [{article}]: "
                f"BRP -> {row['resolved_manufacturer'] or '(unknown)'} [{mark}]"
            )
        if len(candidates) > limit:
            self.stdout.write(f"  ... и ещё {len(candidates) - limit}")
