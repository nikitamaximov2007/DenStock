"""Доменные операции со структурой склада."""
import re
from decimal import Decimal

from django.apps import apps
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.db.models.deletion import ProtectedError
from django.utils import timezone

from .models import StorageLocation, StorageLocationRenameHistory


class StorageLocationRenameError(ValueError):
    """Ошибка, которую можно показать пользователю формы переименования."""


class StorageLocationCreateError(ValueError):
    """Ожидаемая ошибка создания ячейки через адресный flow."""


class StorageLocationRemovalError(ValueError):
    """Безопасное удаление или архивирование ячейки невозможно."""


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
        if expected_code.strip() != location.code:
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
) -> None:
    """Записать изменение кода и его аудит в одной транзакции."""
    updates = {"code": new_code}
    if new_barcode is not None:
        updates["barcode"] = new_barcode
    StorageLocation.objects.filter(pk=location.pk).update(**updates)
    StorageLocationRenameHistory.objects.create(
        location=location,
        old_code=old_code,
        new_code=new_code,
        renamed_by=by,
    )


@transaction.atomic
def rename_storage_location(
    location: StorageLocation,
    *,
    new_code: str,
    expected_code: str,
    by=None,
) -> StorageLocation:
    """Переименовать одну существующую ячейку, не меняя её идентичность.

    Связанные остатки и документы продолжают ссылаться на тот же primary key.
    Отдельный снимок ``WarehouseAction.location_code`` намеренно не меняется.
    """
    locked_location = StorageLocation.objects.select_for_update().get(pk=location.pk)
    if expected_code != locked_location.code:
        raise StorageLocationRenameError(
            "Код ячейки уже изменён другим пользователем. Обновите страницу."
        )

    old_code = locked_location.code
    old_barcode = locked_location.barcode
    normalized_code = normalize_storage_location_code(new_code)
    if normalized_code == normalize_storage_location_code(old_code):
        raise StorageLocationRenameError("Новый код совпадает с текущим кодом ячейки.")
    if (
        StorageLocation.objects.filter(code__iexact=normalized_code)
        .exclude(pk=locked_location.pk)
        .exists()
    ):
        raise StorageLocationRenameError("Ячейка с таким кодом уже существует.")

    new_barcode = (
        auto_location_barcode(normalized_code)
        if is_auto_location_barcode(old_barcode, old_code)
        else None
    )
    if new_barcode and (
        StorageLocation.objects.filter(barcode=new_barcode)
        .exclude(pk=locked_location.pk)
        .exists()
    ):
        raise StorageLocationRenameError(
            "Штрихкод для нового кода уже используется другой ячейкой."
        )

    try:
        # Savepoint leaves the outer transaction usable after a concurrent unique conflict.
        with transaction.atomic():
            _persist_location_rename(
                locked_location,
                old_code=old_code,
                new_code=normalized_code,
                new_barcode=new_barcode,
                by=by,
            )
    except IntegrityError as exc:
        raise StorageLocationRenameError(
            "Ячейка с таким кодом или штрихкодом уже существует."
        ) from exc

    locked_location.code = normalized_code
    if new_barcode is not None:
        locked_location.barcode = new_barcode
    return locked_location
