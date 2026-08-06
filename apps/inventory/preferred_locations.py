"""Read-only planning for the initial preferred-cell backfill."""

from collections import defaultdict
from dataclasses import dataclass

from apps.catalog.models import PartType
from apps.warehouse.models import StorageLocation

from .models import PartItem, PartPreferredLocation, StockLot, StockMovement


@dataclass(frozen=True)
class PreferredLocationBackfill:
    part_id: int
    location_id: int | None
    source: str
    location_is_usable: bool = False


_CURRENT_LOT_STATUSES = (StockLot.Status.AVAILABLE, StockLot.Status.QUARANTINE)
_CURRENT_ITEM_STATUSES = (PartItem.Status.AVAILABLE, PartItem.Status.QUARANTINE)
_PLACEMENT_MOVEMENT_TYPES = (
    StockMovement.MovementType.RECEIVE_ITEM,
    StockMovement.MovementType.RECEIVE_LOT,
    StockMovement.MovementType.MOVE_ITEM,
    StockMovement.MovementType.MOVE_LOT,
    StockMovement.MovementType.ADJUST_IN,
    StockMovement.MovementType.RETURN_ITEM,
    StockMovement.MovementType.RETURN_LOT,
)


def build_preferred_location_backfill() -> list[PreferredLocationBackfill]:
    """Plan a deterministic, non-mutating backfill for existing part cards.

    A single current positive location is the strongest signal. Several current
    locations are deliberately reported as ambiguous rather than made into a
    hidden primary cell. If no current stock exists, the latest confirmed
    placement movement is used as the historical fallback.
    """
    current_locations: dict[int, set[int]] = defaultdict(set)
    usable_location_ids = set(
        StorageLocation.objects.filter(is_active=True, storage_allowed=True).values_list(
            "pk", flat=True
        )
    )
    for part_id, location_id in StockLot.objects.filter(
        status__in=_CURRENT_LOT_STATUSES, quantity__gt=0
    ).values_list("part_type_id", "location_id"):
        current_locations[part_id].add(location_id)
    for part_id, location_id in PartItem.objects.filter(
        status__in=_CURRENT_ITEM_STATUSES, current_location__isnull=False
    ).values_list("part_type_id", "current_location_id"):
        current_locations[part_id].add(location_id)

    latest_placements: dict[int, tuple[int, bool]] = {}
    movements = (
        StockMovement.objects.filter(
            movement_type__in=_PLACEMENT_MOVEMENT_TYPES,
            to_location__isnull=False,
        )
        .select_related("to_location")
        .order_by("part_type_id", "-created_at", "-pk")
    )
    for movement in movements:
        latest_placements.setdefault(
            movement.part_type_id,
            (movement.to_location_id, movement.to_location.can_hold_stock()),
        )

    existing_part_ids = set(PartPreferredLocation.objects.values_list("part_type_id", flat=True))
    plan: list[PreferredLocationBackfill] = []
    for part_id in PartType.objects.order_by("pk").values_list("pk", flat=True):
        if part_id in existing_part_ids:
            plan.append(PreferredLocationBackfill(part_id, None, "existing"))
            continue
        locations = current_locations.get(part_id, set())
        if len(locations) == 1:
            location_id = next(iter(locations))
            plan.append(
                PreferredLocationBackfill(
                    part_id, location_id, "current", location_id in usable_location_ids
                )
            )
        elif len(locations) > 1:
            plan.append(PreferredLocationBackfill(part_id, None, "ambiguous"))
        elif placement := latest_placements.get(part_id):
            location_id, usable = placement
            plan.append(PreferredLocationBackfill(part_id, location_id, "history", usable))
        else:
            plan.append(PreferredLocationBackfill(part_id, None, "none"))
    return plan
