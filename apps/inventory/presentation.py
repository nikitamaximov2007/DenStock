"""Read-only helpers for human-friendly inventory presentation."""

from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch, Q

from apps.catalog.models import PartNumber


def identity_numbers_prefetch() -> Prefetch:
    """Prefetch exact/primary warehouse numbers without analog ordering traps."""
    return Prefetch(
        "part_type__numbers",
        queryset=(
            PartNumber.objects.exclude(kind=PartNumber.Kind.ANALOG)
            .order_by("-is_primary", "pk")
        ),
        to_attr="identity_numbers_for_display",
    )


def part_exact_number(part) -> str:
    """Return catalog identity, never replacement/analog/price-source number."""
    try:
        return part.brp_link.brp_part.material_no
    except (AttributeError, ObjectDoesNotExist):
        pass
    try:
        return part.polaris_link.polaris_part.part_number
    except (AttributeError, ObjectDoesNotExist):
        pass

    numbers = getattr(part, "identity_numbers_for_display", None)
    if numbers is None:
        numbers = list(
            PartNumber.objects.filter(part=part)
            .exclude(kind=PartNumber.Kind.ANALOG)
            .order_by("-is_primary", "pk")[:1]
        )
    return numbers[0].value if numbers else "Артикул не указан"


def attach_movement_identity(movements) -> None:
    """Attach exact display snapshots to movements in one action query.

    Scanner sales/repairs and cancellation returns use WarehouseAction's
    immutable part_number snapshot. Other movements fall back to the exact
    catalog identity of their PartType.
    """
    movements = list(movements)
    sale_ids = {
        movement.document_id
        for movement in movements
        if movement.document_id
        and movement.document_type in {"sale", "stock_return"}
    }
    repair_ids = {
        movement.document_id
        for movement in movements
        if movement.document_id and movement.document_type == "repair_order"
    }
    snapshots = {}
    if sale_ids or repair_ids:
        from apps.actions.models import WarehouseAction

        actions = WarehouseAction.objects.filter(
            Q(sale_id__in=sale_ids) | Q(repair_order_id__in=repair_ids)
        ).order_by("pk")
        for action in actions:
            if action.sale_id:
                snapshots[("sale", action.sale_id)] = action.part_number
                snapshots[("stock_return", action.sale_id)] = action.part_number
            if action.repair_order_id:
                snapshots[("repair_order", action.repair_order_id)] = action.part_number

    for movement in movements:
        snapshot = snapshots.get((movement.document_type, movement.document_id))
        movement.display_part_number = snapshot or part_exact_number(movement.part_type)
