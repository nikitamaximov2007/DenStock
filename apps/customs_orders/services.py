"""Canonical source membership and atomic, immutable customs order finalization."""
import datetime
import hashlib
import json
from decimal import ROUND_HALF_UP, Decimal

from django.core import signing
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q

from apps.catalog.services import get_current_price_settings
from apps.warehouse.models import ValuationSettings

from .models import CustomsOrder, CustomsOrderLine

CHANGED = "Состав изменился. Обновите список и повторите."
TOKEN_SALT = "customs-orders.selection.v1"
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)
SNAPSHOT_FIELDS = (
    "number", "name_ru", "name_en", "manufacturer", "country", "gross_weight_kg",
    "net_weight_kg", "application_area", "quantity", "usd_price", "is_analog",
    "provenance", "occurred_at",
)


class CustomsOrderError(ValueError):
    pass


def current_fx_rate() -> Decimal:
    # GET/report/preview must not create settings rows.
    return Decimal(get_current_price_settings(create=False).current_usd_rate)


def line_rub(row, rate):
    if row["usd_price"] is None:
        return None
    return (row["usd_price"] * row["quantity"] * rate).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )


def customs_sources(filters=None, *, unassigned_only=False) -> list[dict]:
    """One row per stable source, with membership excluded in SQL when requested."""
    from apps.actions.customs_history import canonical_customs_lines
    from apps.actions.services import _customs_rows_from_lines
    from apps.ordered_parts.customs import ordered_parts_customs_lines

    filters = dict(filters or {})
    filters["unassigned_only"] = unassigned_only
    lines = canonical_customs_lines(**filters)
    if not filters.get("action_type") and not filters.get("location_code"):
        lines += ordered_parts_customs_lines(**filters)
    memberships = {
        (member.source, member.source_id): member
        for member in CustomsOrderLine.objects.select_related("order")
    } if not unassigned_only else {}
    # Resolve trusted catalog/version values once per part/version/article.
    profiles = {
        row["source_key"]: row for row in _customs_rows_from_lines(lines)
    }
    result = []
    for line in lines:
        marker = (line["kind"], line["line_id"])
        member = memberships.get(marker)
        if line["quantity"] <= 0:
            if member is None:
                continue
            # Fully returned assigned sources still show their exact frozen order.
            row = {
                "number": member.article, "name_ru": member.name_ru, "name_en": member.name_en,
                "manufacturer": member.manufacturer, "country": member.country,
                "gross_weight_kg": member.gross_weight_kg, "net_weight_kg": member.net_weight_kg,
                "application_area": member.application_area, "usd_price": member.wholesale_usd,
            }
        else:
            version = line["version"]
            key = (line["part_id"], version.pk if version is not None else None, line["number"])
            row = dict(profiles[key])
        row.update(
            source=marker[0], source_id=marker[1], occurred_at=line["occurred_at"],
            quantity=line["quantity"], is_analog=bool(line.get("is_analog")),
            provenance="ordered" if marker[0] == "ordered" else "sales_repairs",
            membership=member, document_number=line["document_number"],
        )
        result.append(row)
    return sorted(result, key=lambda row: (
        row["occurred_at"] or _EPOCH, row["source"], row["source_id"]
    ))


def eligible_customs_sources() -> list[dict]:
    return customs_sources(unassigned_only=True)


def _signature(row):
    values = {field: row.get(field) for field in SNAPSHOT_FIELDS}
    digest = hashlib.sha256(json.dumps(values, sort_keys=True, default=str).encode()).hexdigest()
    return [row["source"], row["source_id"], digest]


def selection_payload(sources, *, rate=None):
    """Sign the exact displayed dataset; the browser sends only the boundary."""
    return signing.dumps({
        "rows": [_signature(row) for row in sources],
        "fx": str(current_fx_rate() if rate is None else rate),
    }, salt=TOKEN_SALT, compress=True)


def _selection(token, boundary_source):
    try:
        payload = signing.loads(token, salt=TOKEN_SALT, max_age=7200)
        markers = [(row[0], row[1]) for row in payload["rows"]]
        selected = payload["rows"][:markers.index(boundary_source) + 1]
        if not selected or len(set(markers)) != len(markers):
            raise ValueError
        return selected, Decimal(payload["fx"])
    except (signing.BadSignature, TypeError, KeyError, ValueError) as exc:
        raise CustomsOrderError(CHANGED) from exc


def _lock_sources(selected):
    """Coordinate with the existing cancellation/return locks, without stock writes.

    NOWAIT fails closed if a warehouse transaction already owns a source or
    document, avoiding inversion of the older flows' different lock orders.
    """
    from apps.ordered_parts.models import OrderedPart
    from apps.repairs.models import RepairIssueLine, RepairOrder
    from apps.returns.models import StockReturn
    from apps.sales.models import Sale, SaleLine

    ids = {kind: [row[1] for row in selected if row[0] == kind]
           for kind in ("sale", "repair", "ordered")}

    def lock(query):
        return list(
            query.select_for_update(nowait=True).order_by("pk").values_list("pk", flat=True)
        )

    sales = list(SaleLine.objects.filter(pk__in=ids["sale"]).values_list("sale_id", flat=True))
    repairs = list(RepairIssueLine.objects.filter(pk__in=ids["repair"]).values_list(
        "repair_order_id", flat=True
    ))
    lock(Sale.objects.filter(pk__in=sales))
    lock(RepairOrder.objects.filter(pk__in=repairs))
    lock(StockReturn.objects.filter(
        Q(source_type=StockReturn.SourceType.SALE, source_id__in=sales)
        | Q(source_type=StockReturn.SourceType.REPAIR_ORDER, source_id__in=repairs)
    ))
    for kind, model in (("sale", SaleLine), ("repair", RepairIssueLine), ("ordered", OrderedPart)):
        if len(lock(model.objects.filter(pk__in=ids[kind]))) != len(ids[kind]):
            raise CustomsOrderError(CHANGED)


def _persist(number, selected, rate, by):
    missing = sorted({row["number"] or "без артикула" for row in selected
                      if row["usd_price"] is None})
    if missing:
        raise CustomsOrderError("Нет оптовой цены для артикулов: " + ", ".join(missing) + ".")
    if any(row["usd_price"] <= 0 for row in selected):
        raise CustomsOrderError("Оптовая цена должна быть больше нуля.")
    amounts = [line_rub(row, rate) for row in selected]
    order = CustomsOrder.objects.create(
        number=number, created_by=by, fx_rate=rate,
        total_quantity=sum((row["quantity"] for row in selected), Decimal("0")),
        total_rub=sum(amounts, Decimal("0")),
    )
    CustomsOrderLine.objects.bulk_create([
        CustomsOrderLine(
            order=order, source=row["source"], source_id=row["source_id"], article=row["number"],
            name_ru=row["name_ru"], name_en=row["name_en"], manufacturer=row["manufacturer"],
            country=row["country"], gross_weight_kg=row["gross_weight_kg"],
            net_weight_kg=row["net_weight_kg"], application_area=row["application_area"],
            occurred_at=row["occurred_at"], quantity=row["quantity"],
            wholesale_usd=row["usd_price"],
            rub_amount=amount, is_analog=row["is_analog"], is_ordered=row["source"] == "ordered",
        ) for row, amount in zip(selected, amounts, strict=True)
    ])
    return order


def create_customs_order_from_boundary(*, number, boundary_source, selection_token, by=None):
    try:
        number = int(number)
    except (ValueError, TypeError) as exc:
        raise CustomsOrderError("Номер заказа должен быть положительным числом.") from exc
    if not 0 < number <= 2147483647:
        raise CustomsOrderError(
            "Номер заказа должен быть положительным целым числом до 2147483647."
        )
    expected, displayed_rate = _selection(selection_token, boundary_source)
    try:
        with transaction.atomic():
            settings = ValuationSettings.objects.select_for_update(nowait=True).filter(pk=1).first()
            rate = Decimal(settings.current_usd_rate) if settings else current_fx_rate()
            if rate <= 0 or rate != displayed_rate:
                raise CustomsOrderError("Курс изменился. Обновите список и повторите.")
            if CustomsOrder.objects.filter(number=number).exists():
                raise CustomsOrderError("Заказ с таким номером уже существует.")
            _lock_sources(expected)
            candidates = eligible_customs_sources()
            selected = candidates[:len(expected)]
            if [_signature(row) for row in selected] != expected:
                raise CustomsOrderError(CHANGED)
            return _persist(number, selected, rate, by)
    except IntegrityError as exc:
        if CustomsOrder.objects.filter(number=number).exists():
            raise CustomsOrderError("Заказ с таким номером уже существует.") from exc
        raise CustomsOrderError(CHANGED) from exc
    except OperationalError as exc:
        cause = exc.__cause__
        if getattr(cause, "sqlstate", None) in {"55P03", "40P01", "40001"}:
            raise CustomsOrderError(CHANGED) from exc
        raise


def create_customs_order(*, number, lines, by=None, fx_rate=None):
    """Compatibility entry point, subject to the same canonical prefix validation."""
    if not lines:
        raise CustomsOrderError("Выберите хотя бы одну позицию.")
    return create_customs_order_from_boundary(
        number=number, boundary_source=(lines[-1]["source"], lines[-1]["source_id"]),
        selection_token=selection_payload(lines, rate=fx_rate), by=by,
    )
