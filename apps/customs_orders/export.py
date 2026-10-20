"""Таможенная форма из зафиксированных данных заказа."""

import datetime
from collections import defaultdict
from decimal import Decimal
from io import BytesIO

from apps.actions.services import (
    ORDERED_PROVENANCE,
    SALES_REPAIRS_PROVENANCE,
    export_customs_xlsx,
)

_METADATA_FIELDS = (
    "name_ru",
    "name_en",
    "manufacturer",
    "country",
    "gross_weight_kg",
    "net_weight_kg",
    "application_area",
)
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)


def _identity_value(value) -> str:
    """Stable comparison value for a frozen supplier article or manufacturer."""
    return " ".join((value or "").split()).upper()


def _metadata_signature(line) -> tuple:
    """Only stated metadata participates; blanks never invent a fact."""
    return tuple(getattr(line, field) for field in _METADATA_FIELDS)


def _chronological_key(line) -> tuple:
    """Match the Customs history order for the frozen source operation."""
    return (line.occurred_at or _EPOCH, line.source, line.source_id)


def _matches_signature(partial: tuple, complete: tuple) -> bool:
    return all(value in (None, "") or value == candidate
               for value, candidate in zip(partial, complete, strict=True))


def _line_row(line) -> dict:
    return {
            "number": line.article,
            "name_ru": line.name_ru,
            "name_en": line.name_en,
            "manufacturer": line.manufacturer,
            "country": line.country,
            "gross_weight_kg": line.gross_weight_kg,
            "net_weight_kg": line.net_weight_kg,
            "quantity": line.quantity,
            "usd_price": line.wholesale_usd,
            "application_area": line.application_area,
            "provenance": ORDERED_PROVENANCE if line.is_ordered else SALES_REPAIRS_PROVENANCE,
            "is_analog": line.is_analog,
            "_chronological_key": _chronological_key(line),
        }


def _normalized_manufacturer(value) -> str:
    return "".join((value or "").upper().replace("-", "").split())


def _catalog_name_map(lines, parts):
    """Resolve only exact, manufacturer-qualified catalog names in bounded queries."""
    from apps.brp.models import BrpCatalogPart
    from apps.catalog.models import normalize_number
    from apps.catalog_import.models import AftermarketCatalogPart
    from apps.polaris.models import PolarisCatalogPart

    exact_keys = {
        (normalize_number(line.article), _normalized_manufacturer(line.manufacturer))
        for line in lines
        if line.part_type_id and line.article and line.manufacturer
    }
    numbers = {number for number, _manufacturer in exact_keys if number}
    candidates = defaultdict(list)
    for row in BrpCatalogPart.objects.filter(material_no_norm__in=numbers, is_current=True):
        candidates[(row.material_no_norm, "BRP")].append(row.part_desc.strip())
    for row in PolarisCatalogPart.objects.filter(part_number_norm__in=numbers):
        candidates[(row.part_number_norm, "POLARIS")].append(row.part_name.strip())
    for row in AftermarketCatalogPart.objects.filter(
        normalized_manufacturer_number__in=numbers
    ).select_related("manufacturer"):
        candidates[(row.normalized_manufacturer_number,
                    _normalized_manufacturer(row.manufacturer.name))].append(
            row.source_description.strip()
        )
    # Normalization removes punctuation. If that makes two catalog records
    # collide for one maker, an unlinked row has no safe catalog identity.
    by_key = {key: values[0] for key, values in candidates.items() if len(values) == 1}

    result = {}
    for line in lines:
        part = parts.get(line.part_type_id)
        if part is None:
            continue
        # A supplier link is the strongest identity. Do not fall through to an
        # article match if the frozen manufacturer conflicts with that link.
        linked = None
        link_brand = ""
        if hasattr(part, "brp_link"):
            linked = part.brp_link.brp_part.part_desc
            link_brand = "BRP"
        elif hasattr(part, "polaris_link"):
            linked = part.polaris_link.polaris_part.part_name
            link_brand = "POLARIS"
        elif hasattr(part, "aftermarket_catalog_entry"):
            entry = part.aftermarket_catalog_entry
            linked = entry.source_description
            link_brand = _normalized_manufacturer(entry.manufacturer.name)
        expected_brand = _normalized_manufacturer(line.manufacturer)
        if linked is not None:
            if not expected_brand or expected_brand == link_brand:
                result[line.pk] = linked.strip()
            continue
        number = normalize_number(line.article)
        if number and expected_brand:
            result[line.pk] = (by_key.get((number, expected_brand)) or "").strip()
    return result


def _fallback_names(lines):
    """Fill absent name snapshots from the same stable part and trusted sources.

    Existing order snapshots always win. The fallback is read-only and only
    applies when a historical line has no name; catalog matches use a stable
    PartType link or exact article plus manufacturer, never a similar title.
    """
    from apps.actions.models import PartCustomsDataVersion, PartCustomsInfo
    from apps.actions.services import MANUAL_CATEGORY_NAME, auto_customs_name_ru
    from apps.catalog.models import PartType

    missing = [line for line in lines if (not line.name_ru.strip() or not line.name_en.strip())]
    part_ids = {line.part_type_id for line in missing if line.part_type_id}
    if not part_ids:
        return

    parts = {
        part.pk: part for part in PartType.objects.filter(pk__in=part_ids).select_related(
            "category", "brp_link__brp_part", "polaris_link__polaris_part",
            "aftermarket_catalog_entry__manufacturer",
        )
    }
    customs = {
        item.part_type_id: item
        for item in PartCustomsInfo.objects.filter(part_type_id__in=part_ids)
    }
    versions = {part_id: [] for part_id in part_ids}
    for version in PartCustomsDataVersion.objects.filter(part_type_id__in=part_ids).order_by(
        "part_type_id", "effective_from", "version"
    ):
        versions[version.part_type_id].append(version)
    catalog_names = _catalog_name_map(missing, parts)

    for line in missing:
        part = parts.get(line.part_type_id)
        if part is None:
            continue
        history = versions.get(part.pk, [])
        at = line.occurred_at
        effective = [version for version in history if at is None or version.effective_from <= at]
        version = effective[-1] if effective else (history[0] if history else None)
        current = customs.get(part.pk)

        english = (
            (version.customs_name_en.strip() if version else "")
            or (current.customs_name_en.strip() if current else "")
            or catalog_names.get(line.pk, "")
        )
        russian = (version.customs_name_ru.strip() if version else "")
        if not russian and current and current.customs_name_ru_confirmed:
            russian = current.customs_name_ru.strip()
        if not russian and english:
            russian = auto_customs_name_ru(english)
        if not russian and not (
            hasattr(part, "brp_link") or hasattr(part, "polaris_link")
            or hasattr(part, "aftermarket_catalog_entry")
        ) and part.category.name == MANUAL_CATEGORY_NAME:
            russian = part.name.strip()

        if not line.name_ru.strip() and russian:
            line.name_ru = russian.upper()
        if not line.name_en.strip() and english:
            line.name_en = english.upper()


def _aggregate_lines(lines) -> list[dict]:
    """Aggregate identical frozen customs rows without changing their facts.

    A unit-price difference is always a separate row: combining it would alter
    the declared total.  Conflicting non-empty metadata is also kept separate.
    A partial row joins a fully specified row only when there is exactly one
    compatible metadata signature; otherwise it remains an explicit row.
    """
    rows = []
    buckets = defaultdict(list)
    for line in lines:
        # An absent article has no canonical supplier/article identity.  It is
        # still a legitimate frozen source row, but combining two such rows
        # would manufacture an identity that the historical data never had.
        article = _identity_value(line.article)
        if not article:
            rows.append(_line_row(line))
            continue
        buckets[(
            article, _identity_value(line.manufacturer),
            line.is_analog, line.wholesale_usd,
        )].append(line)

    for lines_for_identity in buckets.values():
        signatures = {_metadata_signature(line) for line in lines_for_identity}
        groups = defaultdict(list)
        for line in lines_for_identity:
            signature = _metadata_signature(line)
            matches = [
                candidate for candidate in signatures
                if _matches_signature(signature, candidate)
            ]
            highest_detail = max(sum(value not in (None, "") for value in candidate)
                                 for candidate in matches)
            best = [candidate for candidate in matches if sum(
                value not in (None, "") for value in candidate
            ) == highest_detail]
            # A partial row can promote blanks only if one most-specific
            # signature fits.  Tied alternatives mean a genuine conflict and
            # must not be silently attributed to either article row.
            key = best[0] if len(best) == 1 else signature
            groups[key].append(line)

        for group in groups.values():
            row = _line_row(group[0])
            for field in _METADATA_FIELDS:
                values = [getattr(line, field) for line in group]
                row[field] = next((value for value in values if value not in (None, "")), None)
            row["quantity"] = sum((line.quantity for line in group), Decimal("0"))
            # The grouping key guarantees a single unit price.  Preserve that
            # exact snapshot value rather than recalculating it from totals.
            row["usd_price"] = group[0].wholesale_usd
            # Green highlighting has no financial meaning.  It is retained if
            # any source row was a part ordered specifically for a customer.
            row["provenance"] = (
                ORDERED_PROVENANCE if any(line.is_ordered for line in group)
                else SALES_REPAIRS_PROVENANCE
            )
            # A merged row represents its earliest source operation, just as
            # the Customs history does.  Keep the full key for stable ties.
            row["_chronological_key"] = min(_chronological_key(line) for line in group)
            rows.append(row)
    return rows


def export_customs_order_xlsx(order) -> BytesIO:
    """Create frozen order sheets, enriching only missing names at export time."""
    originals, analogs = [], []
    lines = list(order.lines.all())
    _fallback_names(lines)
    for row in _aggregate_lines(lines):
        (analogs if row.pop("is_analog", False) else originals).append(row)
    for section in (originals, analogs):
        section.sort(key=lambda row: row["_chronological_key"])
        for row in section:
            row.pop("_chronological_key")
    return export_customs_xlsx(sheet_rows=[("Оригиналы", originals), ("Аналоги", analogs)])
