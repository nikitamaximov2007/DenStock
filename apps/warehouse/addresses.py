"""Canonical Storage Address V2 helpers.

Новые адреса DenisStock имеют только форму S-D-C. Старые S-L-D-C читаются
отдельным parser и никогда не преобразуются без явного physical mapping.
"""
import re
from dataclasses import dataclass

from django.db import IntegrityError, transaction

from .models import StorageLocation, StorageLocationAlias
from .services import (
    StorageLocationCreateError,
    StorageLocationRenameError,
    _assert_location_identity_available,
)

_V2_RE = re.compile(
    r"^S(?P<rack>\d+)"
    r"(?:-D(?P<drawer>\d+))?"
    r"(?:-C(?P<cell>\d+))?$",
    re.IGNORECASE,
)
_LEGACY_RE = re.compile(
    r"^(?:(?P<zone>[A-ZА-ЯЁ0-9]+)-)?"
    r"S(?P<rack>\d+)-L(?P<level>\d+)"
    r"(?:-(?P<kind>[DBKX])(?P<unit>\d+))?"
    r"(?:-C(?P<cell>\d+))?$",
    re.IGNORECASE,
)


class AddressError(ValueError):
    """Некорректный или неоднозначный складской адрес."""


@dataclass(frozen=True)
class StorageAddress:
    rack: int
    drawer: int | None = None
    cell: int | None = None

    @property
    def code(self) -> str:
        return compose_address(
            self.rack,
            drawer_no=self.drawer,
            cell_no=self.cell,
        )

    @property
    def level(self) -> str:
        if self.cell is not None:
            return StorageLocation.Level.CELL
        if self.drawer is not None:
            return StorageLocation.Level.DRAWER
        return StorageLocation.Level.RACK

    @property
    def parent_code(self) -> str | None:
        if self.cell is not None:
            return compose_address(self.rack, drawer_no=self.drawer)
        if self.drawer is not None:
            return compose_address(self.rack)
        return None


@dataclass(frozen=True)
class LegacyStorageAddress:
    raw_code: str
    zone: str
    rack: int
    level_number: int
    kind: str | None
    unit_number: int | None
    cell_number: int | None

    @property
    def drawer_code(self) -> str | None:
        if self.kind is None or self.unit_number is None:
            return None
        prefix = f"{self.zone}-" if self.zone else ""
        return (
            f"{prefix}S{self.rack:02d}-L{self.level_number:02d}-"
            f"{self.kind}{self.unit_number:02d}"
        )


def _positive_number(value, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AddressError(f"{label} должен быть целым числом.") from exc
    if number < 1:
        raise AddressError(f"{label} начинается с 1.")
    return number


def compose_address(
    rack: int,
    *,
    drawer_no: int | None = None,
    cell_no: int | None = None,
) -> str:
    """Собрать новый canonical S-D-C адрес без пользовательского уровня L."""
    rack = _positive_number(rack, "Номер стеллажа")
    parts = [f"S{rack:02d}"]
    if drawer_no is not None:
        drawer_no = _positive_number(drawer_no, "Номер ящика")
        parts.append(f"D{drawer_no:02d}")
    if cell_no is not None:
        if drawer_no is None:
            raise AddressError("Ячейка должна находиться внутри ящика.")
        cell_no = _positive_number(cell_no, "Номер ячейки")
        parts.append(f"C{cell_no:02d}")
    return "-".join(parts)


def parse_address(raw: str) -> StorageAddress:
    """Разобрать только canonical V2 code и вернуть нормализованные номера."""
    code = (raw or "").strip().upper()
    match = _V2_RE.fullmatch(code)
    if match is None:
        raise AddressError("Ожидается адрес вида S03, S03-D02 или S03-D02-C05.")
    rack = _positive_number(match.group("rack"), "Номер стеллажа")
    drawer = (
        _positive_number(match.group("drawer"), "Номер ящика")
        if match.group("drawer")
        else None
    )
    cell = (
        _positive_number(match.group("cell"), "Номер ячейки")
        if match.group("cell")
        else None
    )
    if cell is not None and drawer is None:
        raise AddressError("Ячейка должна находиться внутри ящика.")
    return StorageAddress(rack=rack, drawer=drawer, cell=cell)


def parse_legacy_address(raw: str) -> LegacyStorageAddress:
    """Разобрать legacy S-L-D/B/K/X-C без вывода нового physical mapping."""
    code = (raw or "").strip().upper()
    match = _LEGACY_RE.fullmatch(code)
    if match is None:
        raise AddressError("Код не является поддерживаемым legacy S-L адресом.")
    return LegacyStorageAddress(
        raw_code=code,
        zone=match.group("zone") or "",
        rack=_positive_number(match.group("rack"), "Номер стеллажа"),
        level_number=_positive_number(match.group("level"), "Номер уровня"),
        kind=match.group("kind"),
        unit_number=(
            _positive_number(match.group("unit"), "Номер legacy места")
            if match.group("unit")
            else None
        ),
        cell_number=(
            _positive_number(match.group("cell"), "Номер ячейки")
            if match.group("cell")
            else None
        ),
    )


def is_v2_address(raw: str) -> bool:
    try:
        parse_address(raw)
    except AddressError:
        return False
    return True


def _identity_owner(code: str):
    canonical = StorageLocation.objects.filter(code__iexact=code).first()
    alias = StorageLocationAlias.objects.filter(code__iexact=code).select_related(
        "location"
    ).first()
    if canonical and alias and canonical.pk != alias.location_id:
        raise StorageLocationCreateError(
            "Код неоднозначен: он одновременно canonical и historical alias."
        )
    return canonical or (alias.location if alias else None)


def _get_or_create_canonical_parent(address: StorageAddress) -> StorageLocation:
    canonical = StorageLocation.objects.filter(code__iexact=address.code).first()
    if canonical is not None:
        if canonical.level != address.level:
            raise StorageLocationCreateError(
                f"Родитель {address.code} существует с другим типом места."
            )
        if not canonical.is_active:
            raise StorageLocationCreateError(f"Родитель {address.code} архивирован.")
        return canonical
    if StorageLocationAlias.objects.filter(code__iexact=address.code).exists():
        raise StorageLocationCreateError(
            f"Родитель {address.code} является старым адресом. Используйте текущий адрес."
        )
    return _create_v2_location(address, name=address.code)


def _create_v2_location(address: StorageAddress, *, name: str) -> StorageLocation:
    existing = _identity_owner(address.code)
    if existing is not None:
        return existing
    parent = None
    if address.parent_code:
        parent_address = parse_address(address.parent_code)
        parent = _get_or_create_canonical_parent(parent_address)
    barcode = f"LOC:{address.code}"
    try:
        _assert_location_identity_available(code=address.code, barcode=barcode)
    except StorageLocationRenameError as exc:
        raise StorageLocationCreateError(str(exc)) from exc
    try:
        with transaction.atomic():
            return StorageLocation.objects.create(
                code=address.code,
                barcode=barcode,
                name=name or address.code,
                level=address.level,
                parent=parent,
                storage_allowed=address.level == StorageLocation.Level.CELL,
                sort_order=address.cell or address.drawer or address.rack,
            )
    except IntegrityError as exc:
        existing = _identity_owner(address.code)
        if existing is not None:
            return existing
        raise StorageLocationCreateError(
            "Не удалось создать место: code или barcode уже используется."
        ) from exc


def create_location(
    address: str,
    *,
    name: str = "",
    purpose=StorageLocation.Purpose.NORMAL,
    description: str = "",
    capacity: int | None = None,
) -> StorageLocation:
    """Создать новый V2 target, при необходимости создав только его родителей."""
    parsed = parse_address(address)
    if _identity_owner(parsed.code) is not None:
        raise StorageLocationCreateError("Место с таким адресом уже существует.")
    parent = None
    if parsed.parent_code:
        parent_address = parse_address(parsed.parent_code)
        parent = _get_or_create_canonical_parent(parent_address)
    barcode = f"LOC:{parsed.code}"
    try:
        _assert_location_identity_available(code=parsed.code, barcode=barcode)
    except StorageLocationRenameError as exc:
        raise StorageLocationCreateError(str(exc)) from exc
    try:
        with transaction.atomic():
            return StorageLocation.objects.create(
                code=parsed.code,
                barcode=barcode,
                name=name or parsed.code,
                level=parsed.level,
                parent=parent,
                purpose=purpose,
                storage_allowed=parsed.level == StorageLocation.Level.CELL,
                sort_order=parsed.cell or parsed.drawer or parsed.rack,
                description=description,
                capacity=capacity,
            )
    except IntegrityError as exc:
        raise StorageLocationCreateError(
            "Место с таким code или barcode уже существует."
        ) from exc


def get_or_create_location(
    address: str,
    *,
    name: str = "",
    allow_legacy: bool = False,
) -> StorageLocation:
    """Найти canonical/alias либо создать новый V2 location.

    Legacy creation разрешена только явно для совместимого старого workflow.
    Она не выполняет никакого S-L -> S-D mapping.
    """
    code = (address or "").strip().upper()
    existing = _identity_owner(code)
    if existing is not None:
        return existing
    try:
        parsed = parse_address(code)
    except AddressError as exc:
        if not allow_legacy:
            raise AddressError(
                "Новые места создаются только в формате S-D-C. "
                "Legacy S-L адрес сначала нужно сопоставить через dry-run V2."
            ) from exc
        parse_legacy_address(code)
        level = StorageLocation.Level.CELL if "-C" in code else StorageLocation.Level.SHELF
        try:
            with transaction.atomic():
                return StorageLocation.objects.create(
                    code=code,
                    name=name or code,
                    level=level,
                )
        except IntegrityError as exc:
            existing = _identity_owner(code)
            if existing is not None:
                return existing
            raise StorageLocationCreateError(
                "Не удалось создать legacy-место: code или barcode уже используется."
            ) from exc
    return _create_v2_location(parsed, name=name)
