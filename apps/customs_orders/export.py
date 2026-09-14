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
    """Create two aggregated sheets using only immutable order snapshots."""
    originals, analogs = [], []
    for row in _aggregate_lines(order.lines.all()):
        (analogs if row.pop("is_analog", False) else originals).append(row)
    for section in (originals, analogs):
        section.sort(key=lambda row: row["_chronological_key"])
        for row in section:
            row.pop("_chronological_key")
    return export_customs_xlsx(sheet_rows=[("Оригиналы", originals), ("Аналоги", analogs)])
