"""Session-backed queue for scanner receiving of ordinary part numbers."""

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from decimal import Decimal

from django.db.models import Count, Q

from apps.actions.services import stock_overview
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.brp.pricing import customer_price_rub as brp_customer_price_rub
from apps.catalog.models import PartNumber, PartType, normalize_number
from apps.catalog.services import get_current_price_settings
from apps.counting.services import find_brp_price_source
from apps.inventory.models import StockLot
from apps.inventory.presentation import part_exact_number
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink
from apps.polaris.pricing import customer_price_rub as polaris_customer_price_rub
from apps.polaris.services import find_polaris_price_source
from apps.warehouse.models import StorageLocation

QUEUE_SESSION_KEY = "batch_receiving_queue_v1"
PENDING_SESSION_KEY = "batch_receiving_candidates_v1"


class ReceivingQueueError(Exception):
    pass


@dataclass(frozen=True)
class ReceivingCandidate:
    source: str
    source_id: int
    exact_number: str
    manufacturer: str
    name: str
    part_id: int | None
    unit_price: Decimal | None
    tracking_mode: str = PartType.TrackingMode.BULK

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def session_dict(self) -> dict:
        data = asdict(self)
        data["unit_price"] = str(self.unit_price or Decimal("0"))
        return data


def _brp_candidate(part: BrpCatalogPart, pricing) -> ReceivingCandidate:
    link = (
        BrpPartLink.objects.filter(brp_part=part)
        .select_related("part")
        .order_by("pk")
        .first()
    )
    warehouse_part = link.part if link else None
    price = warehouse_part.recommended_price if warehouse_part else None
    if warehouse_part is None:
        source = find_brp_price_source(part.material_no_norm, part)
        retail = source.retail_price_usd if source else part.retail_price_usd
        price = brp_customer_price_rub(
            retail, pricing.current_usd_rate, pricing.brp_markup_percent
        )
    return ReceivingCandidate(
        source="brp",
        source_id=part.pk,
        exact_number=part.material_no,
        manufacturer="BRP",
        name=part.part_desc or f"BRP {part.material_no}",
        part_id=warehouse_part.pk if warehouse_part else None,
        unit_price=price,
    )


def _polaris_candidate(part: PolarisCatalogPart, pricing) -> ReceivingCandidate:
    link = (
        PolarisPartLink.objects.filter(polaris_part=part)
        .select_related("part")
        .order_by("pk")
        .first()
    )
    warehouse_part = link.part if link else None
    price = warehouse_part.recommended_price if warehouse_part else None
    if warehouse_part is None:
        source = find_polaris_price_source(part.part_number_norm, part)
        retail = source.retail_price_usd if source else part.retail_price_usd
        price = polaris_customer_price_rub(
            retail, pricing.current_usd_rate, pricing.polaris_markup_percent
        )
    return ReceivingCandidate(
        source="polaris",
        source_id=part.pk,
        exact_number=part.part_number,
        manufacturer="POLARIS",
        name=part.part_name or f"Polaris {part.part_number}",
        part_id=warehouse_part.pk if warehouse_part else None,
        unit_price=price,
    )


def _warehouse_candidate(part: PartType, exact_number: str = "") -> ReceivingCandidate:
    return ReceivingCandidate(
        source="warehouse",
        source_id=part.pk,
        exact_number=exact_number or part_exact_number(part),
        manufacturer=part.manufacturer.name if part.manufacturer else "СКЛАД",
        name=part.name,
        part_id=part.pk,
        unit_price=part.recommended_price,
        tracking_mode=part.tracking_mode,
    )


def find_receiving_candidates(raw: str, *, warehouse_part_id: int | None = None) -> list:
    """Find exact warehouse/BRP/Polaris identities, excluding replacement fields."""
    norm = normalize_number(raw)
    pricing = get_current_price_settings()
    candidates: list[ReceivingCandidate] = []

    brp = BrpCatalogPart.objects.filter(material_no_norm=norm).first() if norm else None
    if brp is not None:
        candidates.append(_brp_candidate(brp, pricing))
    polaris = (
        PolarisCatalogPart.objects.filter(part_number_norm=norm).first() if norm else None
    )
    if polaris is not None:
        candidates.append(_polaris_candidate(polaris, pricing))

    matched_numbers = []
    if norm:
        matched_numbers = list(
            PartNumber.objects.filter(normalized_value=norm)
            .exclude(kind=PartNumber.Kind.ANALOG)
            .select_related(
                "part__manufacturer",
                "part__brp_link__brp_part",
                "part__polaris_link__polaris_part",
            )
            .order_by("-is_primary", "pk")
        )
    seen_parts = {candidate.part_id for candidate in candidates if candidate.part_id}
    for number in matched_numbers:
        if number.part_id in seen_parts:
            continue
        if hasattr(number.part, "brp_link") or hasattr(number.part, "polaris_link"):
            continue
        candidates.append(_warehouse_candidate(number.part, number.value))
        seen_parts.add(number.part_id)

    if not candidates and warehouse_part_id is not None:
        part = (
            PartType.objects.filter(pk=warehouse_part_id)
            .select_related(
                "manufacturer", "brp_link__brp_part", "polaris_link__polaris_part"
            )
            .first()
        )
        if part is not None:
            candidates.append(_warehouse_candidate(part))

    unique = {candidate.key: candidate for candidate in candidates}
    return sorted(unique.values(), key=lambda item: (item.manufacturer, item.exact_number))


def resolve_queue_reference(entry: dict) -> ReceivingCandidate:
    """Re-read a queued reference from the database before assignment/posting."""
    source = entry.get("source")
    source_id = entry.get("source_id")
    pricing = get_current_price_settings()
    if source == "brp":
        part = BrpCatalogPart.objects.filter(pk=source_id).first()
        if part is None:
            raise ReceivingQueueError("Позиция BRP больше не найдена.")
        return _brp_candidate(part, pricing)
    if source == "polaris":
        part = PolarisCatalogPart.objects.filter(pk=source_id).first()
        if part is None:
            raise ReceivingQueueError("Позиция Polaris больше не найдена.")
        return _polaris_candidate(part, pricing)
    if source == "warehouse":
        part = (
            PartType.objects.filter(pk=source_id)
            .select_related(
                "manufacturer", "brp_link__brp_part", "polaris_link__polaris_part"
            )
            .first()
        )
        if part is None:
            raise ReceivingQueueError("Карточка детали больше не найдена.")
        exact = (entry.get("exact_number") or "").strip()
        valid = PartNumber.objects.filter(
            part=part, normalized_value=normalize_number(exact)
        ).exclude(kind=PartNumber.Kind.ANALOG).exists()
        if not valid:
            exact = part_exact_number(part)
        return _warehouse_candidate(part, exact)
    raise ReceivingQueueError("Неизвестный источник детали в очереди.")


def _empty_queue() -> dict:
    return {"version": 1, "next_order": 1, "lines": {}, "group_tokens": {}}


def load_queue(session) -> dict:
    queue = session.get(QUEUE_SESSION_KEY)
    if not isinstance(queue, dict) or queue.get("version") != 1:
        queue = _empty_queue()
    queue.setdefault("lines", {})
    queue.setdefault("group_tokens", {})
    queue.setdefault("next_order", 1)
    return queue


def save_queue(session, queue: dict) -> None:
    session[QUEUE_SESSION_KEY] = queue
    session.modified = True


def _serialized_locations(part_id: int | None) -> list[dict]:
    if not part_id:
        return []
    overview = stock_overview(PartType.objects.get(pk=part_id))
    return [
        {
            "id": row["location"].pk,
            "code": row["location"].code,
            "name": row["location"].name,
            "physical": str(row["physical"]),
            "reserved": str(row["reserved"]),
            "available": str(row["available"]),
        }
        for row in overview["locations"]
    ]


def _queue_merge_key(source: str, source_id: int, exact_number: str, location_id) -> tuple:
    """Stable identity of a receiving line, including the scanned exact number."""
    return source, source_id, normalize_number(exact_number), location_id


def add_candidate(session, candidate: ReceivingCandidate) -> tuple[dict, bool]:
    if candidate.tracking_mode != PartType.TrackingMode.BULK:
        raise ReceivingQueueError(
            "Эта карточка учитывается по экземплярам. Сканируйте ITEM:/DS-номер или серийник."
        )
    queue = load_queue(session)
    choices = _serialized_locations(candidate.part_id)
    location_id = choices[0]["id"] if len(choices) == 1 else None
    candidate_key = _queue_merge_key(
        candidate.source, candidate.source_id, candidate.exact_number, location_id
    )
    existing = next(
        (
            line
            for line in queue["lines"].values()
            if _queue_merge_key(
                line["source"],
                line["source_id"],
                line.get("exact_number", ""),
                line.get("location_id"),
            )
            == candidate_key
        ),
        None,
    )
    if existing is not None:
        existing["quantity"] += 1
        existing.update(
            manufacturer=candidate.manufacturer,
            name=candidate.name,
            unit_price=str(candidate.unit_price or Decimal("0")),
            exact_number=candidate.exact_number,
            location_choices=choices,
            location_mode="existing" if choices else "new",
            location_recommended=len(choices) == 1,
        )
        added_new = False
        line = existing
    else:
        line_id = secrets.token_urlsafe(9)
        line = {
            "id": line_id,
            "source": candidate.source,
            "source_id": candidate.source_id,
            "part_id": candidate.part_id,
            "exact_number": candidate.exact_number,
            "manufacturer": candidate.manufacturer,
            "name": candidate.name,
            "unit_price": str(candidate.unit_price or Decimal("0")),
            "quantity": 1,
            "location_id": location_id,
            "location_choices": choices,
            "location_mode": "existing" if choices else "new",
            "location_recommended": len(choices) == 1,
            "created_order": queue["next_order"],
        }
        queue["next_order"] += 1
        queue["lines"][line_id] = line
        added_new = True
    queue["group_tokens"] = {}
    save_queue(session, queue)
    return line, added_new


def store_pending_candidates(session, candidates: list[ReceivingCandidate]) -> str:
    token = secrets.token_urlsafe(16)
    session[PENDING_SESSION_KEY] = {
        "token": token,
        "candidates": {candidate.key: candidate.session_dict() for candidate in candidates},
    }
    session.modified = True
    return token


def pop_pending_candidate(session, token: str, key: str) -> ReceivingCandidate:
    pending = session.get(PENDING_SESSION_KEY) or {}
    if not token or token != pending.get("token"):
        raise ReceivingQueueError("Выбор устарел. Отсканируйте артикул ещё раз.")
    data = (pending.get("candidates") or {}).get(key)
    if not data:
        raise ReceivingQueueError("Выбранная деталь не найдена.")
    session.pop(PENDING_SESSION_KEY, None)
    session.modified = True
    data["unit_price"] = Decimal(data.get("unit_price") or "0")
    return ReceivingCandidate(**data)


def pending_context(session) -> dict:
    pending = session.get(PENDING_SESSION_KEY) or {}
    candidates = list((pending.get("candidates") or {}).values())
    return {"token": pending.get("token", ""), "candidates": candidates}


def clear_pending(session) -> None:
    if PENDING_SESSION_KEY in session:
        session.pop(PENDING_SESSION_KEY, None)
        session.modified = True


def remove_line(session, line_id: str) -> None:
    queue = load_queue(session)
    if queue["lines"].pop(line_id, None) is None:
        raise ReceivingQueueError("Строка очереди не найдена.")
    queue["group_tokens"] = {}
    save_queue(session, queue)


def update_quantity(session, line_id: str, raw_quantity) -> None:
    try:
        quantity = int(raw_quantity)
    except (TypeError, ValueError) as exc:
        raise ReceivingQueueError("Количество должно быть положительным целым числом.") from exc
    if quantity <= 0:
        raise ReceivingQueueError("Количество должно быть положительным целым числом.")
    queue = load_queue(session)
    line = queue["lines"].get(line_id)
    if line is None:
        raise ReceivingQueueError("Строка очереди не найдена.")
    line["quantity"] = quantity
    queue["group_tokens"] = {}
    save_queue(session, queue)


def clear_queue(session) -> None:
    save_queue(session, _empty_queue())
    session.pop(PENDING_SESSION_KEY, None)


def assign_location(session, line_id: str, *, location_id=None, location_code="") -> str:
    queue = load_queue(session)
    line = queue["lines"].get(line_id)
    if line is None:
        raise ReceivingQueueError("Строка очереди не найдена.")
    candidate = resolve_queue_reference(line)
    location = None
    if location_id:
        location = StorageLocation.objects.filter(pk=location_id).first()
    if location is None and location_code:
        location = StorageLocation.objects.filter(code__iexact=location_code.strip()).first()
    if location is None or not location.can_hold_stock():
        raise ReceivingQueueError("Выберите существующую активную ячейку для хранения.")

    choices = _serialized_locations(candidate.part_id)
    allowed = {choice["id"] for choice in choices}
    if allowed and location.pk not in allowed:
        raise ReceivingQueueError("Для этой детали выберите одну из ячеек текущего остатка.")

    line["exact_number"] = candidate.exact_number
    target_key = _queue_merge_key(
        line["source"], line["source_id"], line["exact_number"], location.pk
    )
    duplicate = next(
        (
            other
            for other in queue["lines"].values()
            if other["id"] != line_id
            and _queue_merge_key(
                other["source"],
                other["source_id"],
                other.get("exact_number", ""),
                other.get("location_id"),
            )
            == target_key
        ),
        None,
    )
    if duplicate:
        duplicate["quantity"] += line["quantity"]
        queue["lines"].pop(line_id)
    else:
        line["location_id"] = location.pk
        line["part_id"] = candidate.part_id
        line["location_choices"] = choices
        line["location_recommended"] = False
    queue["group_tokens"] = {}
    save_queue(session, queue)
    return location.code


def unassign_location(session, line_id: str) -> None:
    queue = load_queue(session)
    line = queue["lines"].get(line_id)
    if line is None:
        raise ReceivingQueueError("Строка очереди не найдена.")
    candidate = resolve_queue_reference(line)
    choices = _serialized_locations(candidate.part_id)
    line["location_id"] = None
    line["part_id"] = candidate.part_id
    line["location_choices"] = choices
    line["location_mode"] = "existing" if choices else "new"
    line["location_recommended"] = False
    queue["group_tokens"] = {}
    save_queue(session, queue)


def _line_context(line: dict) -> dict:
    row = dict(line)
    row["unit_price_decimal"] = Decimal(line.get("unit_price") or "0")
    row["total_price"] = row["unit_price_decimal"] * Decimal(line["quantity"])
    return row


def _group_fingerprint(lines: list[dict]) -> str:
    payload = [
        {
            "id": line["id"],
            "source": line["source"],
            "source_id": line["source_id"],
            "exact": line["exact_number"],
            "quantity": line["quantity"],
        }
        for line in sorted(lines, key=lambda item: item["id"])
    ]
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def queue_context(session) -> dict:
    queue = load_queue(session)
    active_locations = list(
        StorageLocation.objects.filter(is_active=True, storage_allowed=True)
        .annotate(
            part_kinds=Count(
                "stock_lots__part_type",
                filter=Q(
                    stock_lots__status__in=(
                        StockLot.Status.AVAILABLE,
                        StockLot.Status.QUARANTINE,
                    )
                ),
                distinct=True,
            )
        )
        .order_by("code")
    )
    location_map = {location.pk: location for location in active_locations}
    groups: dict[int, dict] = {}
    unassigned = []
    for line in queue["lines"].values():
        row = _line_context(line)
        location = location_map.get(line.get("location_id"))
        if location is None:
            unassigned.append(row)
            continue
        group = groups.setdefault(location.pk, {"location": location, "lines": []})
        group["lines"].append(row)

    tokens_changed = False
    result_groups = []
    for location_id, group in groups.items():
        group["lines"].sort(key=lambda line: (line["exact_number"], line["created_order"]))
        group["quantity"] = sum(line["quantity"] for line in group["lines"])
        group["position_count"] = len(group["lines"])
        fingerprint = _group_fingerprint(group["lines"])
        saved = queue["group_tokens"].get(str(location_id))
        if not saved or saved.get("fingerprint") != fingerprint:
            saved = {"fingerprint": fingerprint, "token": secrets.token_urlsafe(24)}
            queue["group_tokens"][str(location_id)] = saved
            tokens_changed = True
        group["token"] = saved["token"]
        result_groups.append(group)
    result_groups.sort(key=lambda group: group["location"].code)
    unassigned.sort(key=lambda line: (line["created_order"], line["exact_number"]))
    if tokens_changed:
        save_queue(session, queue)
    return {
        "groups": result_groups,
        "unassigned": unassigned,
        "active_locations": active_locations,
        "line_count": len(queue["lines"]),
        "quantity": sum(line["quantity"] for line in queue["lines"].values()),
    }


def group_for_post(session, location_id: int, token: str) -> tuple[list[dict], str]:
    queue = load_queue(session)
    saved = queue["group_tokens"].get(str(location_id)) or {}
    if not token or token != saved.get("token"):
        raise ReceivingQueueError("Группа изменилась или форма устарела. Обновите страницу.")
    lines = [
        line for line in queue["lines"].values() if line.get("location_id") == location_id
    ]
    if not lines or _group_fingerprint(lines) != saved.get("fingerprint"):
        raise ReceivingQueueError("Группа изменилась или уже была проведена.")
    return lines, saved["fingerprint"]


def remove_posted_group(session, location_id: int) -> None:
    queue = load_queue(session)
    queue["lines"] = {
        key: line
        for key, line in queue["lines"].items()
        if line.get("location_id") != location_id
    }
    queue["group_tokens"].pop(str(location_id), None)
    save_queue(session, queue)
