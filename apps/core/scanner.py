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

from apps.inventory.models import PartItem
from apps.procurement.models import Batch
from apps.warehouse.models import StorageLocation

from .part_lookup import PartLookupCandidate, clean_lookup_value, resolve_part_lookup


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
    exact_number: str = ""
    manufacturer: str = ""
    category: str = ""
    match_source: str = ""
    is_alias: bool = False
    physical: object = 0
    available: object = 0
    reserved: object = 0
    quarantine: object = 0
    locations: list = field(default_factory=list)

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


def _location_payload(candidate: PartLookupCandidate) -> list[dict]:
    return [
        {
            "id": row.location.pk,
            "code": row.location.code,
            "physical": row.physical,
            "available": row.available,
            "reserved": row.reserved,
            "quarantine": row.quarantine,
        }
        for row in candidate.location_rows
    ]


def _part_result(candidate: PartLookupCandidate, message: str = "") -> ScanResult:
    part = candidate.part
    return ScanResult(
        status="found", type="part_type", id=part.pk, label=part.name,
        url=reverse("part_detail", args=[part.pk]),
        message=message or candidate.alias_message or "Найдена деталь.",
        exact_number=candidate.exact_number,
        manufacturer=candidate.manufacturer,
        category=candidate.category,
        match_source=candidate.match_source,
        is_alias=candidate.is_alias,
        physical=candidate.physical,
        available=candidate.available,
        reserved=candidate.reserved,
        quarantine=candidate.quarantine,
        locations=_location_payload(candidate),
    )


def _item_candidate(item: PartItem) -> dict:
    return {
        "type": "part_item", "id": item.pk, "label": _item_label(item),
        "url": reverse("item_detail", args=[item.pk]),
    }


def _part_candidate(candidate: PartLookupCandidate) -> dict:
    part = candidate.part
    return {
        "type": "part_type", "id": part.pk, "label": part.name,
        "url": reverse("part_detail", args=[part.pk]),
        "exact_number": candidate.exact_number,
        "manufacturer": candidate.manufacturer,
        "category": candidate.category,
        "match_source": candidate.match_source,
        "is_alias": candidate.is_alias,
        "physical": candidate.physical,
        "available": candidate.available,
        "reserved": candidate.reserved,
        "quarantine": candidate.quarantine,
        "locations": _location_payload(candidate),
    }


# --- Резолвер ----------------------------------------------------------------


def resolve_scan(raw: str, *, user=None) -> ScanResult:
    """Распознать код. Чистая функция: ничего не пишет и не выполняет действий."""
    code = clean_lookup_value(raw)
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

    # 5. Серийный номер (уникален в пределах вида; между видами может быть много).
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

    # 6. Exact warehouse number or factory barcode. Catalog aliases are
    # reference hints and never become scanner operation identity.
    lookup = resolve_part_lookup(code)
    if lookup.found:
        return _part_result(lookup.candidate)
    if lookup.ambiguous:
        return ScanResult(
            status="ambiguous",
            message=lookup.message,
            candidates=[_part_candidate(candidate) for candidate in lookup.candidates],
        )

    # 7. Не распознан.
    return ScanResult(status="unknown", message="Код не распознан.")
