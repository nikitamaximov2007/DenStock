"""Слой 23 — печать складских этикеток (read-only представление существующих кодов).

Вьюхи ничего не меняют: читают объект по pk и рендерят печатный шаблон. Доступ —
под `PRINT_LABELS`. Штрихкоды берутся из уже существующих полей (`PartItem.internal_barcode`,
`StorageLocation.barcode`, `PartBarcode.value`) — те же, что распознаёт сканер (Слой 11).
`StockLot` здесь не печатается принципиально (правило «лот не сканируем»).
"""
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, render

from apps.catalog.models import PartType
from apps.inventory.models import PartItem
from apps.warehouse.models import StorageLocation

from .barcode import safe_code128_svg


def _require_print(request) -> None:
    if not request.user.can_print_labels:
        raise PermissionDenied


def _primary_number(part_type: PartType) -> str:
    """Основной/первый OEM-номер вида детали для подписи (без денежных данных)."""
    number = (
        part_type.numbers.filter(is_primary=True).first()
        or part_type.numbers.filter(kind=part_type.numbers.model.Kind.OEM).first()
        or part_type.numbers.first()
    )
    return number.value if number else ""


def _item_ctx(item: PartItem) -> dict:
    return {
        "internal_number": item.internal_number,
        "internal_barcode": item.internal_barcode,
        "part_name": item.part_type.name,
        "primary_number": _primary_number(item.part_type),
        "batch_number": item.batch.number,
        "location": item.current_location.full_path if item.current_location_id else "",
        "serial_number": item.serial_number,
        "barcode_svg": safe_code128_svg(item.internal_barcode),
    }


def _location_ctx(loc: StorageLocation) -> dict:
    return {
        "code": loc.code,
        "barcode": loc.barcode,
        "full_path": loc.full_path,
        "level": loc.get_level_display(),
        "purpose": loc.get_purpose_display(),
        "is_active": loc.is_active,
        "barcode_svg": safe_code128_svg(loc.barcode),
    }


def _part_ctx(part: PartType) -> dict:
    barcode = part.barcodes.first()
    value = barcode.value if barcode else ""
    return {
        "name": part.name,
        "primary_number": _primary_number(part),
        "manufacturer": str(part.manufacturer) if part.manufacturer_id else "",
        "category": str(part.category) if part.category_id else "",
        "barcode": value,
        "barcode_svg": safe_code128_svg(value),
    }


@login_required
def item_label(request, pk):
    _require_print(request)
    item = get_object_or_404(
        PartItem.objects.select_related("part_type", "batch", "current_location"), pk=pk
    )
    return render(request, "labels/item_label.html", {"items": [_item_ctx(item)]})


@login_required
def items_label(request):
    """Простой batch-print выбранных экземпляров: `?ids=1,2,3` (недоверенные id)."""
    _require_print(request)
    raw = request.GET.get("ids", "")
    ids = [int(token) for token in raw.split(",") if token.strip().isdigit()]
    items = (
        PartItem.objects.filter(pk__in=ids)
        .select_related("part_type", "batch", "current_location")
        .order_by("pk")
    )
    return render(request, "labels/item_label.html", {"items": [_item_ctx(i) for i in items]})


@login_required
def location_label(request, pk):
    _require_print(request)
    loc = get_object_or_404(StorageLocation, pk=pk)
    return render(request, "labels/location_label.html", {"locations": [_location_ctx(loc)]})


@login_required
def part_label(request, pk):
    _require_print(request)
    part = get_object_or_404(
        PartType.objects.select_related("manufacturer", "category"), pk=pk
    )
    return render(request, "labels/part_label.html", {"parts": [_part_ctx(part)]})
