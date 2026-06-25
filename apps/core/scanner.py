"""Единый резолв отсканированного кода (Слой 11).

`resolve_scan(code)` — ЧИСТЫЙ сервис: по строке определяет тип объекта и
возвращает локатор. Без побочных эффектов (журнал `UnresolvedScan` пишет
endpoint, не сервис), без действий (приёмка/перемещение/продажа — следующие слои)
и без себестоимости в результате.

Порядок (утверждён, без ложных совпадений — от глобально-уникального к
многозначному):
  1. префиксные ШК: `ITEM:` (PartItem.internal_barcode), `LOC:` (StorageLocation.barcode)
  2. внутренний номер `DS-…` (PartItem.internal_number)
  3. номер партии `П-…` (Batch.number)
  4. код ячейки (StorageLocation.code)
  5. заводской штрихкод (PartBarcode.value, глобально уникален)
  6. серийный номер (PartItem.serial_number, уникален в пределах вида → может быть
     несколько между видами → ambiguous)
  7. OEM/артикул (PartNumber.normalized_value, не уникален → ambiguous)
  8. иначе — unknown
"""
from dataclasses import dataclass, field

from django.urls import reverse

from apps.catalog.models import PartBarcode, PartNumber, PartType, normalize_number
from apps.inventory.models import PartItem
from apps.procurement.models import Batch
from apps.warehouse.models import StorageLocation


@dataclass
class ScanResult:
    """Результат резолва. Себестоимость в него принципиально не входит."""

    status: str  # "found" | "ambiguous" | "unknown"
    type: str | None = None  # part_item | location | batch | part_type
    id: int | None = None
    label: str = ""
    url: str | None = None
    message: str = ""
    candidates: list = field(default_factory=list)

    @property
    def found(self) -> bool:
        return self.status == "found"

    def to_dict(self) -> dict:
        return {
            "found": self.found,
            "status": self.status,
            "type": self.type,
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "message": self.message,
            "candidates": self.candidates,
        }


# --- Локаторы (label/url без себестоимости) ----------------------------------


def _item_label(item: PartItem) -> str:
    return f"Экземпляр {item.internal_number} · {item.part_type.name}"


def _item_result(item: PartItem, message: str) -> ScanResult:
    return ScanResult(
        status="found", type="part_item", id=item.pk, label=_item_label(item),
        url=reverse("item_detail", args=[item.pk]), message=message,
    )


def _location_result(loc: StorageLocation) -> ScanResult:
    return ScanResult(
        status="found", type="location", id=loc.pk,
        label=f"Ячейка {loc.code} — {loc.name}",
        url=reverse("location_detail", args=[loc.pk]), message="Найдена ячейка склада.",
    )


def _batch_result(batch: Batch) -> ScanResult:
    return ScanResult(
        status="found", type="batch", id=batch.pk, label=f"Партия {batch.number}",
        url=reverse("batch_detail", args=[batch.pk]), message="Найдена партия.",
    )


def _part_result(part: PartType, message: str) -> ScanResult:
    return ScanResult(
        status="found", type="part_type", id=part.pk, label=part.name,
        url=reverse("part_detail", args=[part.pk]), message=message,
    )


def _item_candidate(item: PartItem) -> dict:
    return {
        "type": "part_item", "id": item.pk, "label": _item_label(item),
        "url": reverse("item_detail", args=[item.pk]),
    }


def _part_candidate(part: PartType) -> dict:
    return {
        "type": "part_type", "id": part.pk, "label": part.name,
        "url": reverse("part_detail", args=[part.pk]),
    }


# --- Резолвер ----------------------------------------------------------------


def resolve_scan(raw: str, *, user=None) -> ScanResult:
    """Распознать код. Чистая функция: ничего не пишет и не выполняет действий."""
    code = (raw or "").strip()
    if not code:
        return ScanResult(status="unknown", message="Пустой код.")

    # 1. Префиксные системные штрихкоды.
    item = (
        PartItem.objects.filter(internal_barcode=code).select_related("part_type").first()
    )
    if item:
        return _item_result(item, "Найден экземпляр детали.")
    loc = StorageLocation.objects.filter(barcode=code).first()
    if loc:
        return _location_result(loc)

    # 2. Внутренний номер экземпляра (DS-…).
    item = (
        PartItem.objects.filter(internal_number=code).select_related("part_type").first()
    )
    if item:
        return _item_result(item, "Найден экземпляр детали.")

    # 3. Номер партии (П-…).
    batch = Batch.objects.filter(number=code).first()
    if batch:
        return _batch_result(batch)

    # 4. Код ячейки.
    loc = StorageLocation.objects.filter(code=code).first()
    if loc:
        return _location_result(loc)

    # 5. Заводской штрихкод (глобально уникален).
    barcode = PartBarcode.objects.filter(value=code).select_related("part").first()
    if barcode:
        return _part_result(barcode.part, "Найдено по заводскому штрихкоду.")

    # 6. Серийный номер (уникален в пределах вида; между видами — может быть много).
    items = list(
        PartItem.objects.filter(serial_number=code)
        .select_related("part_type")
        .order_by("pk")[:25]
    )
    if len(items) == 1:
        return _item_result(items[0], "Найден экземпляр по серийному номеру.")
    if len(items) > 1:
        return ScanResult(
            status="ambiguous",
            message="Серийный номер найден у нескольких экземпляров — уточните.",
            candidates=[_item_candidate(i) for i in items],
        )

    # 7. OEM/артикул через нормализацию (не уникален → возможна неоднозначность).
    norm = normalize_number(code)
    if norm:
        part_ids = list(
            PartNumber.objects.filter(normalized_value=norm)
            .values_list("part_id", flat=True)
            .distinct()
        )
        if len(part_ids) == 1:
            part = PartType.objects.get(pk=part_ids[0])
            return _part_result(part, "Найдено по номеру детали.")
        if len(part_ids) > 1:
            parts = PartType.objects.filter(pk__in=part_ids).order_by("pk")
            return ScanResult(
                status="ambiguous",
                message="Номер найден у нескольких видов деталей — уточните.",
                candidates=[_part_candidate(p) for p in parts],
            )

    # 8. Не распознан.
    return ScanResult(status="unknown", message="Код не распознан.")
