"""Доменные операции со структурой склада."""
import re
from decimal import Decimal

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from .models import StorageLocation, StorageLocationAlias, StorageLocationRenameHistory


class StorageLocationRenameError(ValueError):
    """Ошибка, которую можно показать пользователю формы переименования."""


class StorageLocationCreateError(ValueError):
    """Ожидаемая ошибка создания ячейки через адресный flow."""


class StorageLocationRemovalError(ValueError):
    """Безопасное удаление или архивирование ячейки невозможно."""


class StorageLocationResolutionError(ValueError):
    """Code/barcode указывает более чем на одну physical identity."""


_LOCATION_CODE_RE = re.compile(r"^[A-ZА-ЯЁ0-9]+(?:-[A-ZА-ЯЁ0-9]+)*$")


def normalize_storage_location_code(raw_code: str) -> str:
    """Нормализовать совместимый с существующими адресами код ячейки.

    Новые составные адреса собирает ``compose_address``. Эта проверка также
    сохраняет читаемость легаси-кодов вроде ``A`` и ``03``.
    """
    code = (raw_code or "").strip().upper()
    if not code:
        raise StorageLocationRenameError("Укажите новый код ячейки.")
    if len(code) > StorageLocation._meta.get_field("code").max_length:
        raise StorageLocationRenameError("Код ячейки слишком длинный.")
    if not _LOCATION_CODE_RE.fullmatch(code):
        raise StorageLocationRenameError(
            "Код ячейки может содержать буквы, цифры и дефисы без пробелов."
        )
    if code.isdigit() and len(code) > 2:
        raise StorageLocationRenameError(
            "Номер детали нельзя использовать как код ячейки."
        )
    return code


def auto_location_barcode(code: str) -> str:
    """Штрихкод, управляемый кодом ячейки."""
    return f"LOC:{code}"


def is_auto_location_barcode(barcode: str, code: str) -> bool:
    """Определить, можно ли безопасно обновить штрихкод при переименовании."""
    return not barcode or barcode == auto_location_barcode(normalize_storage_location_code(code))


def resolve_storage_location(
    raw: str, *, allow_code: bool = True, allow_barcode: bool = True
) -> tuple[StorageLocation | None, bool]:
    """Точно разрешить canonical code/barcode или уникальный historical alias."""
    from .addresses import normalize_address_input

    value = (raw or "").strip().upper()
    if not value:
        return None, False
    # Сотрудник вводит и сканирует операторский адрес 1-1-1, а на старых
    # ярлыках, в распечатках и документах стоит S01-D01-C01. Это один и тот же
    # адрес, и находить он обязан одну и ту же ячейку. Ничего не создаётся:
    # короткая форма лишь приводится к хранимому виду перед поиском.
    values = {value, normalize_address_input(value)}
    canonical_filter = Q()
    alias_filter = Q()
    for candidate in values:
        if allow_code:
            canonical_filter |= Q(code__iexact=candidate)
            alias_filter |= Q(code__iexact=candidate)
        if allow_barcode:
            canonical_filter |= Q(barcode__iexact=candidate)
            alias_filter |= Q(barcode__iexact=candidate)
    if not allow_code and not allow_barcode:
        return None, False
    canonical = list(StorageLocation.objects.filter(canonical_filter)[:2])
    if canonical:
        if len(canonical) > 1:
            raise StorageLocationResolutionError(
                "Адрес неоднозначно совпал с несколькими текущими ячейками."
            )
        return canonical[0], False
    aliases = list(
        StorageLocationAlias.objects.filter(is_active=True)
        .filter(alias_filter)
        .select_related("location")[:2]
    )
    owners = {alias.location_id: alias.location for alias in aliases}
    if len(owners) > 1:
        raise StorageLocationResolutionError(
            "Адрес неоднозначен: canonical и historical alias относятся к разным ячейкам."
        )
    if not owners:
        return None, False
    location = next(iter(owners.values()))
    return location, True


def _assert_location_identity_available(
    *,
    code: str,
    barcode: str | None,
    exclude_location_id: int | None = None,
    allow_alias_reclaim: bool = False,
) -> None:
    canonical = StorageLocation.objects.all()
    aliases = StorageLocationAlias.objects.filter(is_active=True)
    if exclude_location_id is not None:
        canonical = canonical.exclude(pk=exclude_location_id)
        aliases = aliases.exclude(location_id=exclude_location_id)
    code_collision = Q(code__iexact=code) | Q(barcode__iexact=code)
    if canonical.filter(code_collision).exists():
        raise StorageLocationRenameError("Ячейка с таким кодом уже существует.")
    if not allow_alias_reclaim and aliases.filter(code_collision).exists():
        raise StorageLocationRenameError("Historical alias с таким кодом уже существует.")
    if barcode and (
        canonical.filter(Q(code__iexact=barcode) | Q(barcode__iexact=barcode)).exists()
    ):
        raise StorageLocationRenameError("Штрихкод уже используется другой ячейкой.")
    if (
        barcode
        and not allow_alias_reclaim
        and aliases.filter(Q(code__iexact=barcode) | Q(barcode__iexact=barcode)).exists()
    ):
        raise StorageLocationRenameError("Штрихкод уже используется historical alias.")


def create_location_alias(
    location: StorageLocation,
    *,
    code: str,
    barcode: str | None,
    kind=StorageLocationAlias.Kind.RENAME,
    by=None,
) -> StorageLocationAlias:
    """Сохранить старый адрес без возможности направить его на другую identity."""
    code = normalize_storage_location_code(code)
    barcode = barcode.strip().upper() if barcode else None
    existing = StorageLocationAlias.objects.filter(
        is_active=True, code__iexact=code
    ).first()
    if existing is not None:
        if existing.location_id != location.pk or (
            barcode and existing.barcode != barcode
        ):
            raise StorageLocationRenameError(
                "Historical alias уже принадлежит другой ячейке."
            )
        return existing
    if (
        StorageLocation.objects.filter(Q(code__iexact=code) | Q(barcode__iexact=code))
        .exclude(pk=location.pk)
        .exists()
        or StorageLocationAlias.objects.filter(
            Q(code__iexact=code) | Q(barcode__iexact=code), is_active=True
        )
        .exclude(location_id=location.pk)
        .exists()
    ):
        raise StorageLocationRenameError(
            "Старый код уже занят другой ячейкой или alias."
        )
    if barcode and (
        StorageLocation.objects.filter(
            Q(code__iexact=barcode) | Q(barcode__iexact=barcode)
        )
        .exclude(pk=location.pk)
        .exists()
        or StorageLocationAlias.objects.filter(
            Q(code__iexact=barcode) | Q(barcode__iexact=barcode), is_active=True
        )
        .exclude(location_id=location.pk)
        .exists()
    ):
        raise StorageLocationRenameError(
            "Старый штрихкод уже занят другой ячейкой или alias."
        )
    return StorageLocationAlias.objects.create(
        location=location,
        code=code,
        barcode=barcode,
        kind=kind,
        created_by=by,
    )


def _retire_aliases_claimed_by_current_identity(*, code: str, barcode: str | None) -> None:
    """Навсегда убрать повторно выданный historical address из operational lookup."""
    claimed = Q(code__iexact=code) | Q(barcode__iexact=code)
    if barcode:
        claimed |= Q(code__iexact=barcode) | Q(barcode__iexact=barcode)
    StorageLocationAlias.objects.select_for_update().filter(
        is_active=True
    ).filter(claimed).update(is_active=False)


def location_code_at(location: StorageLocation | None, moment) -> str:
    """Восстановить code Location на момент исторической операции."""
    if location is None:
        return ""
    code = location.code
    history = location.rename_history.filter(renamed_at__gt=moment).order_by(
        "-renamed_at", "-pk"
    )
    for entry in history:
        code = entry.old_code
    return code


def attach_movement_location_history(movements) -> None:
    """Attach address-at-event and current address without changing the ledger."""
    movements = list(movements)
    location_ids = {
        location_id
        for movement in movements
        for location_id in (movement.from_location_id, movement.to_location_id)
        if location_id is not None
    }
    history_by_location = {}
    histories = StorageLocationRenameHistory.objects.filter(
        location_id__in=location_ids
    ).order_by("location_id", "-renamed_at", "-pk")
    for entry in histories:
        history_by_location.setdefault(entry.location_id, []).append(entry)

    def code_at(location, moment):
        if location is None:
            return ""
        code = location.code
        for entry in history_by_location.get(location.pk, []):
            if entry.renamed_at > moment:
                code = entry.old_code
        return code

    for movement in movements:
        movement.from_location_historical_code = code_at(
            movement.from_location, movement.created_at
        )
        movement.to_location_historical_code = code_at(
            movement.to_location, movement.created_at
        )
        movement.from_location_was_renamed = bool(
            movement.from_location
            and movement.from_location_historical_code != movement.from_location.code
        )
        movement.to_location_was_renamed = bool(
            movement.to_location
            and movement.to_location_historical_code != movement.to_location.code
        )


def _location_reference_counts(location: StorageLocation) -> list[dict]:
    references = []
    for model in apps.get_models():
        for field in model._meta.fields:
            remote = getattr(field, "remote_field", None)
            if remote is None or remote.model is not StorageLocation:
                continue
            count = model._base_manager.filter(**{field.name: location}).count()
            if count:
                references.append(
                    {
                        "model": model._meta.label,
                        "label": f"{model._meta.verbose_name}: {field.verbose_name}",
                        "count": count,
                    }
                )
    return references


def storage_location_removal_preview(location: StorageLocation) -> dict:
    """Read-only preflight, включая скрытые related_name='+' связи."""
    from apps.inventory.models import PartItem, StockBalance, StockLocationLock, StockLot
    from apps.inventory.services import ITEM_PHYSICAL_STATUSES, LOT_PHYSICAL_STATUSES
    from apps.sales.models import Reservation, ReservationLine

    lot_totals = StockLot.objects.filter(
        location=location, status__in=LOT_PHYSICAL_STATUSES, quantity__gt=0
    ).aggregate(
        physical=Sum("quantity"),
        quarantine=Sum("quantity", filter=Q(status=StockLot.Status.QUARANTINE)),
    )
    lot_physical = lot_totals["physical"] or Decimal("0")
    items = PartItem.objects.filter(
        current_location=location, status__in=ITEM_PHYSICAL_STATUSES
    )
    item_physical = Decimal(items.count())
    quarantine = (lot_totals["quarantine"] or Decimal("0")) + Decimal(
        items.filter(status=PartItem.Status.QUARANTINE).count()
    )
    cached = StockBalance.objects.filter(location=location).aggregate(
        physical=Sum("quantity_physical"),
        available=Sum("quantity_available"),
        reserved=Sum("quantity_reserved"),
    )
    expiry = Q(reservation__expires_at__isnull=True) | Q(
        reservation__expires_at__gt=timezone.now()
    )
    live_reserved = (
        ReservationLine.objects.filter(
            Q(stock_lot__location=location) | Q(part_item__current_location=location),
            reservation__status=Reservation.Status.ACTIVE,
        )
        .filter(expiry)
        .aggregate(total=Sum("quantity"))["total"]
        or Decimal("0")
    )
    physical = max(lot_physical + item_physical, cached["physical"] or Decimal("0"))
    reserved = max(live_reserved, cached["reserved"] or Decimal("0"))
    available = max(
        cached["available"] or Decimal("0"),
        physical - quarantine - reserved,
        Decimal("0"),
    )
    references = _location_reference_counts(location)
    active_lock = StockLocationLock.objects.filter(
        location=location, released_at__isnull=True
    ).exists()
    active_children = location.children.filter(is_active=True).count()
    has_stock = physical > 0 or available > 0 or reserved > 0
    return {
        "code": location.code,
        "physical": physical,
        "available": available,
        "reserved": reserved,
        "references": references,
        "reference_count": sum(item["count"] for item in references),
        "has_history": bool(references),
        "active_lock": active_lock,
        "active_children": active_children,
        "has_stock": has_stock,
        "can_hard_delete": not has_stock and not references and not active_lock,
        "can_archive": not has_stock and not active_lock and active_children == 0,
    }


def remove_or_archive_storage_location(
    location: StorageLocation, *, action: str, expected_code: str
) -> tuple[str, str]:
    """Удалить новую пустую ячейку либо деактивировать историческую."""
    with transaction.atomic():
        location = StorageLocation.objects.select_for_update().get(pk=location.pk)
        if location.level != StorageLocation.Level.CELL:
            raise StorageLocationRemovalError(
                "Через этот экран можно удалить или архивировать только ячейку."
            )
        # Подтверждение принимает и операторский 1-1-1, и хранимый S01-D01-C01:
        # сотрудник видит на экране короткую форму и вводит именно её.
        from .addresses import normalize_address_input

        confirmed = normalize_address_input(expected_code)
        if confirmed != location.code and expected_code.strip() != location.code:
            raise StorageLocationRemovalError(
                "Для подтверждения введите точный код ячейки."
            )
        preview = storage_location_removal_preview(location)
        if preview["has_stock"]:
            raise StorageLocationRemovalError(
                "В ячейке есть физический, доступный или зарезервированный остаток. "
                "Сначала переместите или корректно обнулите его."
            )
        code = location.code
        if action == "delete":
            if not preview["can_hard_delete"]:
                raise StorageLocationRemovalError(
                    "Ячейка уже использовалась. Hard delete запрещён; доступно только "
                    "безопасное архивирование."
                )
            try:
                location.delete()
            except ProtectedError as exc:
                raise StorageLocationRemovalError(
                    "Удаление заблокировано историческими ссылками."
                ) from exc
            return "deleted", code
        if action != "archive":
            raise StorageLocationRemovalError("Неизвестное действие с ячейкой.")
        if not preview["can_archive"]:
            raise StorageLocationRemovalError(
                "Ячейку нельзя архивировать: проверьте вложенные места или активный пересчёт."
            )
        location.is_active = False
        location.storage_allowed = False
        location.save(update_fields=["is_active", "storage_allowed", "updated_at"])
        return "archived", code


def _persist_location_rename(
    location: StorageLocation,
    *,
    old_code: str,
    new_code: str,
    new_barcode: str | None,
    by,
    reason=StorageLocationRenameHistory.Reason.MANUAL,
    operation_key: str = "",
) -> None:
    """Записать изменение кода и его аудит в одной транзакции."""
    updates = {"code": new_code}
    if new_barcode is not None:
        updates["barcode"] = new_barcode
    _retire_aliases_claimed_by_current_identity(code=new_code, barcode=new_barcode)
    create_location_alias(
        location,
        code=old_code,
        barcode=(location.barcode if new_barcode is not None else None),
        kind=(
            StorageLocationAlias.Kind.ADDRESS_V2
            if reason == StorageLocationRenameHistory.Reason.ADDRESS_V2
            else StorageLocationAlias.Kind.DRAWER
            if reason == StorageLocationRenameHistory.Reason.DRAWER
            else StorageLocationAlias.Kind.RENAME
        ),
        by=by,
    )
    promoted_filter = Q(code__iexact=new_code)
    if new_barcode is not None:
        promoted_filter |= Q(barcode__iexact=new_barcode)
    StorageLocationAlias.objects.filter(location=location, is_active=True).filter(
        promoted_filter
    ).update(is_active=False)
    StorageLocation.objects.filter(pk=location.pk).update(**updates)
    StorageLocationRenameHistory.objects.create(
        location=location,
        old_code=old_code,
        new_code=new_code,
        renamed_by=by,
        reason=reason,
        operation_key=operation_key,
    )


def _require_cell_address(raw_code: str):
    """Parse the operator's target as a canonical S-D-C cell address."""
    from .addresses import AddressError, normalize_address_input, parse_address

    code = normalize_address_input((raw_code or "").strip().upper())
    if not code:
        raise StorageLocationRenameError("Укажите новый адрес ячейки.")
    try:
        address = parse_address(code)
    except AddressError as exc:
        raise StorageLocationRenameError(
            "Новый адрес должен быть ячейкой в формате S-D-C, например 3-2-7 или S03-D02-C07."
        ) from exc
    if address.cell is None:
        raise StorageLocationRenameError(
            "Новый адрес должен указывать ячейку (S-D-C), а не стеллаж или ящик."
        )
    return address


def historical_cell_move(raw: str):
    """Describe an address a cell was physically moved away from, if that is all it is.

    Returns ``(old_code, current_location)`` or ``None``. The old address is
    kept only as a NON-active alias: it never resolves as a live location, but
    a scan of an old label can explain where the cell went.
    """
    from .addresses import normalize_address_input

    value = (raw or "").strip().upper()
    if not value:
        return None
    terms = {value, normalize_address_input(value)}
    match = Q()
    for term in terms:
        match |= Q(code__iexact=term) | Q(barcode__iexact=term)
    if StorageLocation.objects.filter(match).exists():
        return None
    if StorageLocationAlias.objects.filter(match, is_active=True).exists():
        return None
    alias = (
        StorageLocationAlias.objects.filter(
            match, is_active=False, kind=StorageLocationAlias.Kind.RENAME
        )
        .select_related("location")
        .order_by("-created_at", "-pk")
        .first()
    )
    return (alias.code, alias.location) if alias else None


@transaction.atomic
def rebind_storage_cell(
    location: StorageLocation,
    *,
    new_code: str,
    expected_code: str,
    by=None,
) -> StorageLocation:
    """Move one physical cell to another S-D-C address: detach old, bind new.

    The cell keeps its identity (primary key), so the stock, serial items,
    balances and preferred-location links that live in it follow it to the new
    address without any stock movement. The old address stops being a live
    location: its code and barcode are kept only as a historical, non-active
    alias, so they never resolve for new stock operations and the old place
    can be created again as a new cell. The cell is re-parented under the
    target drawer. An occupied target, including an address another cell still
    answers to, is refused. Audit: ``StorageLocationRenameHistory``.
    """
    from apps.inventory.models import StockLocationLock

    from .addresses import _get_or_create_canonical_parent, _sort_order, parse_address

    locked_location = StorageLocation.objects.select_for_update().get(pk=location.pk)
    if locked_location.level != StorageLocation.Level.CELL:
        raise StorageLocationRenameError(
            "Перенести на другой адрес можно только ячейку. "
            "Ящик переименовывается вместе с дочерними ячейками."
        )
    if expected_code != locked_location.code:
        raise StorageLocationRenameError(
            "Адрес ячейки уже изменён другим пользователем. Обновите страницу."
        )
    address = _require_cell_address(new_code)
    old_code = locked_location.code
    if address.code == old_code.upper():
        raise StorageLocationRenameError("Новый адрес совпадает с текущим адресом ячейки.")
    if StockLocationLock.objects.filter(
        location=locked_location, released_at__isnull=True
    ).exists():
        raise StorageLocationRenameError(
            "Ячейка сейчас в пересчёте участка. Перенесите её после завершения пересчёта."
        )

    old_barcode = locked_location.barcode
    new_barcode = (
        auto_location_barcode(address.code)
        if is_auto_location_barcode(old_barcode, old_code)
        else None
    )
    target_identity = Q(code__iexact=address.code) | Q(barcode__iexact=address.code)
    if new_barcode:
        target_identity |= Q(code__iexact=new_barcode) | Q(barcode__iexact=new_barcode)
    list(
        StorageLocation.objects.select_for_update()
        .filter(target_identity)
        .exclude(pk=locked_location.pk)
        .order_by("pk")
    )
    list(
        StorageLocationAlias.objects.select_for_update()
        .filter(target_identity, is_active=True)
        .order_by("pk")
    )
    # Fail closed: an address another cell still answers to (its current code
    # or an active alias left by a drawer/V2 relabel) is occupied.
    _assert_location_identity_available(
        code=address.code,
        barcode=new_barcode,
        exclude_location_id=locked_location.pk,
    )
    try:
        parent = _get_or_create_canonical_parent(parse_address(address.parent_code))
    except StorageLocationCreateError as exc:
        raise StorageLocationRenameError(str(exc)) from exc

    try:
        with transaction.atomic():
            StorageLocationAlias.objects.create(
                location=locked_location,
                code=old_code,
                barcode=old_barcode if new_barcode is not None else None,
                kind=StorageLocationAlias.Kind.RENAME,
                is_active=False,
                created_by=by,
            )
            StorageLocationAlias.objects.filter(
                location=locked_location, is_active=True
            ).filter(target_identity).update(is_active=False)
            updates = {
                "code": address.code,
                "parent": parent,
                "sort_order": _sort_order(address),
                "updated_at": timezone.now(),
            }
            if new_barcode is not None:
                updates["barcode"] = new_barcode
            if locked_location.name.strip().upper() == old_code.upper():
                updates["name"] = address.code
            StorageLocation.objects.filter(pk=locked_location.pk).update(**updates)
            StorageLocationRenameHistory.objects.create(
                location=locked_location,
                old_code=old_code,
                new_code=address.code,
                renamed_by=by,
                reason=StorageLocationRenameHistory.Reason.MANUAL,
            )
    except IntegrityError as exc:
        raise StorageLocationRenameError(
            "Адрес или штрихкод уже занят другой ячейкой."
        ) from exc
    return StorageLocation.objects.get(pk=locked_location.pk)
