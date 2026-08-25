"""Atomic metadata rename for a canonical V2 drawer and all child cells."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from uuid import uuid4

from django.db import IntegrityError, transaction

from apps.inventory.models import StockLocationLock

from .addresses import AddressError, compose_address, parse_address
from .models import StorageLocation, StorageLocationAlias, StorageLocationRenameHistory
from .services import (
    StorageLocationRenameError,
    _assert_location_identity_available,
    _persist_location_rename,
    auto_location_barcode,
    is_auto_location_barcode,
)


@dataclass(frozen=True)
class DrawerCodeChange:
    location_id: int
    old_code: str
    new_code: str
    old_barcode: str
    new_barcode: str
    is_active: bool


@dataclass
class DrawerRenamePlan:
    drawer_id: int
    old_code: str
    new_code: str
    changes: list[DrawerCodeChange] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def can_apply(self) -> bool:
        return not self.conflicts

    def as_dict(self) -> dict:
        return {
            "drawer_id": self.drawer_id,
            "old_code": self.old_code,
            "new_code": self.new_code,
            "changes": [asdict(item) for item in self.changes],
            "conflicts": self.conflicts,
            "can_apply": self.can_apply,
            "fingerprint": self.fingerprint,
        }


def _drawer_number(value) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise StorageLocationRenameError("Номер ящика должен быть целым числом.") from exc
    if number < 0:
        raise StorageLocationRenameError("Номер ящика не может быть отрицательным.")
    return number


def _plan_fingerprint(drawer, children, aliases, locks, new_code) -> str:
    payload = {
        "new_code": new_code,
        "locations": [
            {
                "id": item.pk,
                "code": item.code,
                "barcode": item.barcode,
                "parent_id": item.parent_id,
                "level": item.level,
                "is_active": item.is_active,
                "updated_at": item.updated_at.isoformat(),
            }
            for item in [drawer, *children]
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
        "locks": [
            {"id": item.pk, "location_id": item.location_id, "document_id": item.document_id}
            for item in locks
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_drawer_rename_plan(drawer: StorageLocation, new_number) -> DrawerRenamePlan:
    """Build a read-only preview and reject every ambiguous descendant."""
    drawer = StorageLocation.objects.select_related("parent").get(pk=drawer.pk)
    new_number = _drawer_number(new_number)
    try:
        parsed = parse_address(drawer.code)
    except AddressError as exc:
        raise StorageLocationRenameError(
            "Переименовать ящик можно только для canonical адреса Sxx-Dxx."
        ) from exc
    if (
        drawer.level != StorageLocation.Level.DRAWER
        or parsed.drawer is None
        or parsed.cell is not None
    ):
        raise StorageLocationRenameError(
            "Переименовать ящик можно только для canonical адреса Sxx-Dxx."
        )
    expected_rack = compose_address(parsed.rack)
    if (
        drawer.parent is None
        or drawer.parent.code != expected_rack
        or drawer.parent.level != StorageLocation.Level.RACK
    ):
        raise StorageLocationRenameError("Иерархия ящика не соответствует canonical S-D-C.")
    new_code = compose_address(parsed.rack, drawer_no=new_number)
    if new_code == drawer.code:
        raise StorageLocationRenameError("Новый номер совпадает с текущим номером ящика.")

    children = list(drawer.children.all().order_by("code", "pk"))
    aliases = list(StorageLocationAlias.objects.all().order_by("pk"))
    affected_ids = {drawer.pk, *(child.pk for child in children)}
    locks = list(
        StockLocationLock.objects.filter(
            location_id__in=affected_ids,
            released_at__isnull=True,
        ).order_by("pk")
    )
    plan = DrawerRenamePlan(drawer_id=drawer.pk, old_code=drawer.code, new_code=new_code)
    locations = [drawer, *children]
    proposed_codes = {}
    proposed_barcodes = {}

    for location in locations:
        if location.pk == drawer.pk:
            target_code = new_code
        else:
            try:
                child_address = parse_address(location.code)
            except AddressError:
                plan.conflicts.append(f"Вложенное место {location.code} не является V2-ячейкой.")
                continue
            if (
                location.level != StorageLocation.Level.CELL
                or child_address.rack != parsed.rack
                or child_address.drawer != parsed.drawer
                or child_address.cell is None
            ):
                plan.conflicts.append(
                    f"Вложенное место {location.code} не соответствует ящику {drawer.code}."
                )
                continue
            target_code = f"{new_code}-C{child_address.cell:02d}"
        target_barcode = (
            auto_location_barcode(target_code)
            if is_auto_location_barcode(location.barcode, location.code)
            else location.barcode
        )
        plan.changes.append(
            DrawerCodeChange(
                location_id=location.pk,
                old_code=location.code,
                new_code=target_code,
                old_barcode=location.barcode,
                new_barcode=target_barcode,
                is_active=location.is_active,
            )
        )
        if target_code in proposed_codes:
            plan.conflicts.append(f"Дублирующий target code {target_code}.")
        proposed_codes[target_code] = location.pk
        if target_barcode in proposed_barcodes:
            plan.conflicts.append(f"Дублирующий target barcode {target_barcode}.")
        proposed_barcodes[target_barcode] = location.pk

    for change in plan.changes:
        try:
            _assert_location_identity_available(
                code=change.new_code,
                barcode=change.new_barcode,
                exclude_location_id=change.location_id,
            )
        except StorageLocationRenameError as exc:
            plan.conflicts.append(f"{change.new_code}: {exc}")
        code_owner = (
            StorageLocation.objects.filter(code__iexact=change.new_code)
            .exclude(pk__in=affected_ids)
            .first()
        )
        if code_owner:
            plan.conflicts.append(
                f"Адрес {change.new_code} уже принадлежит Location #{code_owner.pk}."
            )
        barcode_owner = (
            StorageLocation.objects.filter(barcode__iexact=change.new_barcode)
            .exclude(pk__in=affected_ids)
            .first()
        )
        if barcode_owner:
            plan.conflicts.append(
                f"Штрихкод {change.new_barcode} уже принадлежит Location #{barcode_owner.pk}."
            )
        alias_code = next(
            (
                item
                for item in aliases
                if item.code.upper() == change.new_code.upper()
                and item.location_id != change.location_id
            ),
            None,
        )
        if alias_code:
            plan.conflicts.append(
                f"Адрес {change.new_code} занят alias Location #{alias_code.location_id}."
            )
        alias_barcode = next(
            (
                item
                for item in aliases
                if item.barcode
                and item.barcode.upper() == change.new_barcode.upper()
                and item.location_id != change.location_id
            ),
            None,
        )
        if alias_barcode:
            plan.conflicts.append(
                f"Штрихкод {change.new_barcode} занят alias Location "
                f"#{alias_barcode.location_id}."
            )
    for lock in locks:
        plan.conflicts.append(
            f"Location #{lock.location_id} временно заблокирована пересчётом."
        )

    plan.changes.sort(key=lambda item: (item.new_code.count("-"), item.new_code))
    plan.conflicts = sorted(set(plan.conflicts))
    plan.fingerprint = _plan_fingerprint(drawer, children, aliases, locks, new_code)
    return plan


def rename_storage_drawer(
    drawer: StorageLocation,
    *,
    new_number,
    expected_code: str,
    expected_fingerprint: str,
    by=None,
    fault_after: int | None = None,
) -> StorageLocation:
    """Rename drawer and descendants under one transaction without stock mutation."""
    preview = build_drawer_rename_plan(drawer, new_number)
    if expected_code != preview.old_code:
        raise StorageLocationRenameError("Код ящика уже изменился. Обновите страницу.")
    if not expected_fingerprint or expected_fingerprint != preview.fingerprint:
        raise StorageLocationRenameError("Preview устарел. Проверьте переименование ещё раз.")
    if not preview.can_apply:
        raise StorageLocationRenameError("Переименование заблокировано конфликтами.")

    try:
        with transaction.atomic():
            affected_ids = [change.location_id for change in preview.changes]
            list(
                StorageLocation.objects.select_for_update()
                .filter(pk__in=[drawer.parent_id, *affected_ids])
                .order_by("pk")
            )
            list(StorageLocationAlias.objects.select_for_update().order_by("pk"))
            list(
                StockLocationLock.objects.select_for_update()
                .filter(location_id__in=affected_ids, released_at__isnull=True)
                .order_by("pk")
            )
            current = build_drawer_rename_plan(drawer, new_number)
            if current.fingerprint != expected_fingerprint or not current.can_apply:
                raise StorageLocationRenameError(
                    "Структура ящика изменилась после preview; операция остановлена."
                )
            operation_key = uuid4().hex
            for index, change in enumerate(current.changes, start=1):
                location = StorageLocation.objects.get(pk=change.location_id)
                _persist_location_rename(
                    location,
                    old_code=change.old_code,
                    new_code=change.new_code,
                    new_barcode=change.new_barcode,
                    by=by,
                    reason=StorageLocationRenameHistory.Reason.DRAWER,
                    operation_key=operation_key,
                )
                if fault_after is not None and index >= fault_after:
                    raise StorageLocationRenameError("Injected drawer rename fault.")
    except IntegrityError as exc:
        raise StorageLocationRenameError(
            "Переименование столкнулось с новым code/barcode conflict."
        ) from exc
    return StorageLocation.objects.get(pk=drawer.pk)
