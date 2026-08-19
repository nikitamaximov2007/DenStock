"""Auditable repair for a supplier file that zeroed known BRP wholesale prices."""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path

from django.db import transaction
from django.utils import timezone

from apps.catalog.services import (
    get_current_price_settings,
    refresh_linked_part_prices,
)
from apps.catalog_import.adapters import file_sha256
from apps.catalog_import.models import CatalogImportBatch
from apps.catalog_import.services import stored_file_path

from .importer import ZERO, BrpImportError, selected_wholesale_prices
from .models import BrpCatalogPart

CORRECTION_KIND = "brp_zero_wholesale_fallback_v1"
UPDATE_BATCH_SIZE = 1000


class BrpPriceCorrectionError(RuntimeError):
    """The evidence for a safe BRP zero-price correction is insufficient."""


@dataclass
class PriceCorrectionSummary:
    source_batch_id: int
    source_filename: str
    source_sha256: str
    previous_filename: str
    previous_sha256: str
    current_materials: int = 0
    same_file_nonzero: int = 0
    previous_catalog_fallback: int = 0
    no_usable_price: int = 0
    ambiguous_nonzero: int = 0
    invalid_or_negative: int = 0
    rows_to_update: int = 0
    linked_prices_refreshed: int = 0
    status_counts: Counter = field(default_factory=Counter)
    samples: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        result = asdict(self)
        result["status_counts"] = dict(self.status_counts)
        return result


def _usable(value: Decimal | None) -> bool:
    return value is not None and value > ZERO


def _chunks(values: list[str]):
    for start in range(0, len(values), UPDATE_BATCH_SIZE):
        yield values[start:start + UPDATE_BATCH_SIZE]


def _assert_readable_supplier_file(
    path: Path, *, label: str
) -> tuple[dict[str, Decimal | None], object]:
    try:
        prices, summary = selected_wholesale_prices(path)
    except BrpImportError as exc:
        raise BrpPriceCorrectionError(f"Не удалось прочитать {label}: {exc}") from exc
    if summary.invalid_wholesale_price or summary.negative_wholesale_price:
        raise BrpPriceCorrectionError(
            f"В {label} есть некорректные или отрицательные оптовые цены."
        )
    return prices, summary


def plan_zero_wholesale_correction(
    source_batch: CatalogImportBatch, *, previous_file: str | Path
) -> tuple[PriceCorrectionSummary, dict[str, Decimal | None]]:
    """Build a read-only correction plan from two immutable supplier snapshots."""
    if source_batch.catalog != CatalogImportBatch.Catalog.BRP:
        raise BrpPriceCorrectionError("Коррекция поддерживает только BRP-каталог.")
    if source_batch.status != CatalogImportBatch.Status.APPLIED:
        raise BrpPriceCorrectionError("Исходная партия должна быть уже применена.")
    current_file = stored_file_path(source_batch)
    if not current_file.exists() or file_sha256(current_file) != source_batch.source_sha256:
        raise BrpPriceCorrectionError("Файл исходной партии отсутствует или изменён.")

    current_prices, current_summary = _assert_readable_supplier_file(
        current_file, label="новый файл поставщика"
    )
    previous_path = Path(previous_file)
    if not previous_path.exists():
        raise BrpPriceCorrectionError("Предыдущий файл поставщика не найден.")
    previous_prices, previous_summary = _assert_readable_supplier_file(
        previous_path, label="предыдущий файл поставщика"
    )

    summary = PriceCorrectionSummary(
        source_batch_id=source_batch.pk,
        source_filename=source_batch.source_filename,
        source_sha256=source_batch.source_sha256,
        previous_filename=previous_path.name,
        previous_sha256=file_sha256(previous_path),
        current_materials=len(current_prices),
        ambiguous_nonzero=current_summary.conflicting_nonzero_wholesale,
        invalid_or_negative=(
            current_summary.invalid_wholesale_price
            + current_summary.negative_wholesale_price
            + previous_summary.invalid_wholesale_price
            + previous_summary.negative_wholesale_price
        ),
    )

    selected: dict[str, Decimal | None] = {}
    materials = list(current_prices)
    existing = {}
    for chunk in _chunks(materials):
        existing.update(
            {
                obj.material_no: obj
                for obj in BrpCatalogPart.objects.filter(material_no__in=chunk).only(
                    "material_no", "wholesale_price_usd", "brp_status"
                )
            }
        )
    for material, current_price in current_prices.items():
        if _usable(current_price):
            summary.same_file_nonzero += 1
            continue
        old_price = previous_prices.get(material)
        chosen = old_price if _usable(old_price) else None
        if chosen is None:
            summary.no_usable_price += 1
        else:
            summary.previous_catalog_fallback += 1
        obj = existing.get(material)
        if obj is None:
            # A supplier file may contain a material which was intentionally
            # excluded from the applied snapshot. A correction never creates it.
            continue
        summary.status_counts[obj.brp_status or ""] += 1
        if obj.wholesale_price_usd != chosen:
            selected[material] = chosen
            if len(summary.samples) < 25:
                summary.samples.append(
                    {
                        "material_no": material,
                        "new_supplier_wholesale": str(current_price)
                        if current_price is not None
                        else None,
                        "previous_wholesale": str(old_price) if old_price is not None else None,
                        "chosen_wholesale": str(chosen) if chosen is not None else None,
                        "source": "previous_catalog" if chosen is not None else "missing",
                        "status": obj.brp_status,
                    }
                )
    summary.rows_to_update = len(selected)
    return summary, selected


def apply_zero_wholesale_correction(
    source_batch: CatalogImportBatch,
    *,
    previous_file: str | Path,
    by=None,
) -> tuple[CatalogImportBatch, PriceCorrectionSummary]:
    """Apply a reviewed plan and create a new immutable audit batch.

    Only ``wholesale_price_usd`` is changed. The source batch, current supplier
    metadata, stock and historical documents remain untouched.
    """
    with transaction.atomic():
        CatalogImportBatch.objects.select_for_update().filter(
            catalog=CatalogImportBatch.Catalog.BRP
        ).order_by("pk").first()
        source = CatalogImportBatch.objects.select_for_update().get(pk=source_batch.pk)
        summary, selected = plan_zero_wholesale_correction(source, previous_file=previous_file)
        correction = CatalogImportBatch.objects.create(
            catalog=CatalogImportBatch.Catalog.BRP,
            status=CatalogImportBatch.Status.CHECKED,
            source_filename=f"Коррекция нулевых wholesale: batch #{source.pk}",
            source_sha256=source.source_sha256,
            source_size=source.source_size,
            stored_path=source.stored_path,
            catalog_fingerprint="",
            summary={
                "kind": CORRECTION_KIND,
                "created_at": timezone.now().isoformat(),
                **summary.as_dict(),
            },
            created_by=by,
        )
        now = timezone.now()
        for chunk in _chunks(list(selected)):
            objects = list(
                BrpCatalogPart.objects.select_for_update().filter(material_no__in=chunk).only(
                    "id", "material_no", "wholesale_price_usd", "updated_at"
                )
            )
            for obj in objects:
                obj.wholesale_price_usd = selected[obj.material_no]
                obj.updated_at = now
            if objects:
                BrpCatalogPart.objects.bulk_update(
                    objects, ["wholesale_price_usd", "updated_at"], batch_size=UPDATE_BATCH_SIZE
                )
        pricing = get_current_price_settings()
        summary.linked_prices_refreshed = refresh_linked_part_prices(
            usd_rate=pricing.current_usd_rate,
            brp_markup=pricing.brp_markup_percent,
            polaris_markup=pricing.polaris_markup_percent,
            catalogs=frozenset({"brp"}),
        )
        correction.status = CatalogImportBatch.Status.APPLIED
        correction.applied_at = now
        correction.applied_by = by
        correction.apply_summary = {
            "kind": CORRECTION_KIND,
            "applied_at": timezone.now().isoformat(),
            **summary.as_dict(),
        }
        correction.save(update_fields=["status", "applied_at", "applied_by", "apply_summary"])
    return correction, summary
