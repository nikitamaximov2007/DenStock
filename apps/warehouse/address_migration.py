"""Dry-run-first migration of legacy S-L-D-C identities to Storage Address V2."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from django.db import transaction

from apps.inventory.models import StockLocationLock

from .addresses import AddressError, parse_address, parse_legacy_address
from .models import StorageLocation, StorageLocationAlias, StorageLocationRenameHistory
from .services import (
    StorageLocationRenameError,
    _assert_location_identity_available,
    _persist_location_rename,
    auto_location_barcode,
    is_auto_location_barcode,
)


class StorageAddressMigrationError(ValueError):
    """Address V2 plan is unsafe or no longer matches current state."""


@dataclass(frozen=True)
class AddressChange:
    location_id: int
    old_code: str
    new_code: str
    old_barcode: str
    new_barcode: str
    target_parent_code: str
    target_level: str
    is_active: bool


@dataclass
class AddressMigrationPlan:
    mapping: dict[str, str]
    changes: list[AddressChange] = field(default_factory=list)
    create_racks: list[str] = field(default_factory=list)
    create_drawers: list[str] = field(default_factory=list)
    already_applied: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def can_apply(self) -> bool:
        return not self.unmapped and not self.conflicts

    def as_dict(self) -> dict:
        return {
            "mapping": self.mapping,
            "changes": [asdict(item) for item in self.changes],
            "create_racks": self.create_racks,
            "create_drawers": self.create_drawers,
            "already_applied": self.already_applied,
            "unmapped": self.unmapped,
            "conflicts": self.conflicts,
            "can_apply": self.can_apply,
            "fingerprint": self.fingerprint,
        }


def _normalize_mapping(raw_mapping: dict) -> tuple[dict[str, str], list[str]]:
    normalized = {}
    conflicts = []
    if not isinstance(raw_mapping, dict):
        return {}, ["Mapping должен быть JSON object OLD_DRAWER -> NEW_DRAWER."]
    targets = {}
    for old_raw, new_raw in raw_mapping.items():
        old_code = str(old_raw).strip().upper()
        new_code = str(new_raw).strip().upper()
        try:
            old = parse_legacy_address(old_code)
        except AddressError as exc:
            conflicts.append(f"Некорректный legacy drawer {old_code}: {exc}")
            continue
        try:
            new = parse_address(new_code)
        except AddressError as exc:
            conflicts.append(f"Некорректный V2 drawer {new_code}: {exc}")
            continue
        if old.drawer_code != old_code or old.kind != "D" or old.cell_number is not None:
            conflicts.append(f"Mapping key должен быть полным legacy D-ящиком: {old_code}.")
            continue
        if new.drawer is None or new.cell is not None:
            conflicts.append(f"Mapping value должен быть полным V2 D-ящиком: {new_code}.")
            continue
        if new_code in targets and targets[new_code] != old_code:
            conflicts.append(
                f"Ящик {new_code} назначен двум legacy-ящикам: "
                f"{targets[new_code]} и {old_code}."
            )
            continue
        targets[new_code] = old_code
        normalized[old_code] = new_code
    return dict(sorted(normalized.items())), conflicts


def _new_barcode(location: StorageLocation, new_code: str) -> str:
    if is_auto_location_barcode(location.barcode, location.code):
        return auto_location_barcode(new_code)
    return location.barcode


def _plan_fingerprint(plan: AddressMigrationPlan, locations, aliases, locks) -> str:
    payload = {
        "mapping": plan.mapping,
        "locations": [
            {
                "id": item.pk,
                "code": item.code,
                "barcode": item.barcode,
                "level": item.level,
                "parent_id": item.parent_id,
                "is_active": item.is_active,
                "storage_allowed": item.storage_allowed,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in locations
        ],
        "aliases": [
            {
                "id": item.pk,
                "location_id": item.location_id,
                "code": item.code,
                "barcode": item.barcode,
            }
            for item in aliases
        ],
        "active_locks": [
            {"id": item.pk, "location_id": item.location_id, "document_id": item.document_id}
            for item in locks
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_storage_address_v2_plan(raw_mapping: dict) -> AddressMigrationPlan:
    """Build a complete read-only plan; no production mapping is inferred."""
    mapping, mapping_conflicts = _normalize_mapping(raw_mapping)
    plan = AddressMigrationPlan(mapping=mapping, conflicts=mapping_conflicts)
    locations = list(StorageLocation.objects.all().order_by("pk"))
    aliases = list(StorageLocationAlias.objects.all().order_by("pk"))
    locks = list(
        StockLocationLock.objects.filter(released_at__isnull=True).order_by("pk")
    )
    location_by_code = {item.code.upper(): item for item in locations}
    alias_by_code = {item.code.upper(): item for item in aliases}
    alias_by_barcode = {
        item.barcode.upper(): item for item in aliases if item.barcode
    }
    changed_ids = set()
    proposed_codes = {}
    proposed_barcodes = {}
    mapped_groups = {old_code: [] for old_code in mapping}

    for location in locations:
        try:
            legacy = parse_legacy_address(location.code)
        except AddressError:
            continue
        drawer_code = legacy.drawer_code
        if drawer_code is None or legacy.kind != "D":
            plan.unmapped.append(location.code)
            continue
        if drawer_code not in mapping:
            plan.unmapped.append(location.code)
            continue
        new_drawer = mapping[drawer_code]
        new_code = (
            f"{new_drawer}-C{legacy.cell_number:02d}"
            if legacy.cell_number is not None
            else new_drawer
        )
        target_level = (
            StorageLocation.Level.CELL
            if legacy.cell_number is not None
            else StorageLocation.Level.DRAWER
        )
        new_barcode = _new_barcode(location, new_code)
        change = AddressChange(
            location_id=location.pk,
            old_code=location.code,
            new_code=new_code,
            old_barcode=location.barcode,
            new_barcode=new_barcode,
            target_parent_code=(
                new_drawer
                if legacy.cell_number is not None
                else new_drawer.rsplit("-", 1)[0]
            ),
            target_level=target_level,
            is_active=location.is_active,
        )
        plan.changes.append(change)
        mapped_groups[drawer_code].append(change)
        changed_ids.add(location.pk)
        if new_code in proposed_codes:
            plan.conflicts.append(
                f"Дублирующий target code {new_code}: Location "
                f"#{proposed_codes[new_code]} и #{location.pk}."
            )
        proposed_codes[new_code] = location.pk
        if new_barcode in proposed_barcodes:
            plan.conflicts.append(
                f"Дублирующий target barcode {new_barcode}: Location "
                f"#{proposed_barcodes[new_barcode]} и #{location.pk}."
            )
        proposed_barcodes[new_barcode] = location.pk

    for old_drawer, new_drawer in mapping.items():
        group = mapped_groups[old_drawer]
        if not group:
            prefix = f"{old_drawer}-C"
            related_aliases = [
                item
                for item in aliases
                if item.code == old_drawer or item.code.startswith(prefix)
            ]
            if related_aliases and all(
                item.location.code == new_drawer
                or item.location.code.startswith(f"{new_drawer}-C")
                for item in related_aliases
            ):
                plan.already_applied.append(f"{old_drawer} -> {new_drawer}")
            else:
                plan.conflicts.append(f"Legacy-ящик {old_drawer} не найден в canonical данных.")
            continue
        new = parse_address(new_drawer)
        rack_code = f"S{new.rack:02d}"
        rack = location_by_code.get(rack_code)
        if rack is None:
            plan.create_racks.append(rack_code)
        elif rack.level != StorageLocation.Level.RACK:
            plan.conflicts.append(f"Target parent {rack_code} существует, но это не стеллаж.")
        drawer_change = next((item for item in group if item.old_code == old_drawer), None)
        if drawer_change is None:
            plan.create_drawers.append(new_drawer)

    for change in plan.changes:
        location = location_by_code[change.old_code.upper()]
        try:
            _assert_location_identity_available(
                code=change.new_code,
                barcode=change.new_barcode,
                exclude_location_id=location.pk,
            )
        except StorageLocationRenameError as exc:
            plan.conflicts.append(f"{change.new_code}: {exc}")
        canonical_owner = location_by_code.get(change.new_code.upper())
        if canonical_owner is not None and canonical_owner.pk not in changed_ids:
            plan.conflicts.append(
                f"Target code {change.new_code} уже принадлежит Location #{canonical_owner.pk}."
            )
        alias_owner = alias_by_code.get(change.new_code.upper())
        if alias_owner is not None and alias_owner.location_id != location.pk:
            plan.conflicts.append(
                f"Target code {change.new_code} занят alias Location #{alias_owner.location_id}."
            )
        canonical_barcode_owner = next(
            (
                item
                for item in locations
                if item.barcode.upper() == change.new_barcode.upper()
                and item.pk not in changed_ids
            ),
            None,
        )
        if canonical_barcode_owner is not None:
            plan.conflicts.append(
                f"Target barcode {change.new_barcode} занят Location "
                f"#{canonical_barcode_owner.pk}."
            )
        alias_barcode_owner = alias_by_barcode.get(change.new_barcode.upper())
        if alias_barcode_owner is not None and alias_barcode_owner.location_id != location.pk:
            plan.conflicts.append(
                f"Target barcode {change.new_barcode} занят alias Location "
                f"#{alias_barcode_owner.location_id}."
            )
        old_alias = alias_by_code.get(change.old_code.upper())
        if old_alias is not None and old_alias.location_id != location.pk:
            plan.conflicts.append(
                f"Legacy code {change.old_code} уже занят alias другой Location."
            )

    locked_ids = {item.location_id for item in locks}
    for change in plan.changes:
        if change.location_id in locked_ids:
            plan.conflicts.append(
                f"Location #{change.location_id} ({change.old_code}) заблокирована пересчётом."
            )

    for code in plan.create_racks + plan.create_drawers:
        try:
            _assert_location_identity_available(
                code=code,
                barcode=auto_location_barcode(code),
            )
        except StorageLocationRenameError as exc:
            plan.conflicts.append(f"{code}: {exc}")
        canonical = location_by_code.get(code.upper())
        if canonical is not None and canonical.pk not in changed_ids:
            plan.conflicts.append(
                f"Создаваемый parent code {code} уже занят Location #{canonical.pk}."
            )
        if code.upper() in alias_by_code:
            plan.conflicts.append(f"Создаваемый parent code {code} занят historical alias.")
        barcode = auto_location_barcode(code)
        canonical_barcode = next(
            (item for item in locations if item.barcode.upper() == barcode.upper()),
            None,
        )
        if canonical_barcode is not None and canonical_barcode.pk not in changed_ids:
            plan.conflicts.append(
                f"Создаваемый parent barcode {barcode} уже занят Location "
                f"#{canonical_barcode.pk}."
            )
        if barcode.upper() in alias_by_barcode:
            plan.conflicts.append(f"Создаваемый parent barcode {barcode} занят historical alias.")

    plan.changes.sort(key=lambda item: (item.new_code.count("-"), item.new_code, item.location_id))
    plan.create_racks = sorted(set(plan.create_racks))
    plan.create_drawers = sorted(set(plan.create_drawers))
    plan.already_applied = sorted(set(plan.already_applied))
    plan.unmapped = sorted(set(plan.unmapped))
    plan.conflicts = sorted(set(plan.conflicts))
    plan.fingerprint = _plan_fingerprint(plan, locations, aliases, locks)
    return plan


def apply_storage_address_v2_plan(
    raw_mapping: dict,
    *,
    expected_fingerprint: str,
    by=None,
    fault_after: int | None = None,
) -> dict:
    """Apply an unchanged safe plan atomically while preserving Location IDs."""
    initial = build_storage_address_v2_plan(raw_mapping)
    if not expected_fingerprint or initial.fingerprint != expected_fingerprint:
        raise StorageAddressMigrationError(
            "Fingerprint dry-run не совпадает с текущим состоянием склада."
        )
    if not initial.can_apply:
        raise StorageAddressMigrationError("Address V2 plan содержит blockers.")
    with transaction.atomic():
        list(StorageLocation.objects.select_for_update().order_by("pk"))
        list(StorageLocationAlias.objects.select_for_update().order_by("pk"))
        list(
            StockLocationLock.objects.select_for_update()
            .filter(released_at__isnull=True)
            .order_by("pk")
        )
        plan = build_storage_address_v2_plan(raw_mapping)
        if plan.fingerprint != expected_fingerprint:
            raise StorageAddressMigrationError(
                "Состояние адресов изменилось после dry-run; apply остановлен."
            )
        if not plan.can_apply:
            raise StorageAddressMigrationError("Address V2 plan больше не безопасен.")

        operation_key = uuid4().hex
        created = []
        for rack_code in plan.create_racks:
            rack = StorageLocation.objects.create(
                name=rack_code,
                code=rack_code,
                level=StorageLocation.Level.RACK,
                storage_allowed=False,
            )
            created.append(rack.code)

        applied = 0
        for old_drawer, new_drawer in plan.mapping.items():
            group = [
                item
                for item in plan.changes
                if item.old_code == old_drawer or item.old_code.startswith(f"{old_drawer}-C")
            ]
            if not group:
                continue
            rack_code = new_drawer.rsplit("-", 1)[0]
            rack = StorageLocation.objects.get(code=rack_code)
            drawer_change = next((item for item in group if item.old_code == old_drawer), None)
            if drawer_change is None:
                drawer = StorageLocation.objects.create(
                    name=new_drawer,
                    code=new_drawer,
                    level=StorageLocation.Level.DRAWER,
                    parent=rack,
                    storage_allowed=False,
                )
                created.append(drawer.code)
            else:
                drawer = StorageLocation.objects.get(pk=drawer_change.location_id)
                _persist_location_rename(
                    drawer,
                    old_code=drawer_change.old_code,
                    new_code=drawer_change.new_code,
                    new_barcode=drawer_change.new_barcode,
                    by=by,
                    reason=StorageLocationRenameHistory.Reason.ADDRESS_V2,
                    operation_key=operation_key,
                )
                StorageLocation.objects.filter(pk=drawer.pk).update(
                    parent=rack,
                    level=StorageLocation.Level.DRAWER,
                    storage_allowed=False,
                )
                applied += 1
            for change in group:
                if change is drawer_change:
                    continue
                location = StorageLocation.objects.get(pk=change.location_id)
                _persist_location_rename(
                    location,
                    old_code=change.old_code,
                    new_code=change.new_code,
                    new_barcode=change.new_barcode,
                    by=by,
                    reason=StorageLocationRenameHistory.Reason.ADDRESS_V2,
                    operation_key=operation_key,
                )
                StorageLocation.objects.filter(pk=location.pk).update(
                    parent=drawer,
                    level=StorageLocation.Level.CELL,
                )
                applied += 1
                if fault_after is not None and applied >= fault_after:
                    raise StorageAddressMigrationError("Injected Address V2 apply fault.")
        return {
            "operation_key": operation_key,
            "updated_locations": applied,
            "created_parents": created,
            "already_applied": plan.already_applied,
        }


def load_address_mapping(path) -> dict:
    try:
        with open(path, encoding="utf-8") as source:
            mapping = json.load(source)
    except (OSError, json.JSONDecodeError) as exc:
        raise StorageAddressMigrationError(f"Не удалось прочитать mapping JSON: {exc}") from exc
    if not isinstance(mapping, dict):
        raise StorageAddressMigrationError("Mapping JSON должен быть object OLD -> NEW.")
    return mapping
