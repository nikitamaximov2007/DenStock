from decimal import Decimal

from django.db import transaction

from apps.warehouse.models import ValuationSettings

from .models import CustomsOrder, CustomsOrderLine


class CustomsOrderError(ValueError):
    pass


def current_fx_rate() -> Decimal:
    return Decimal(ValuationSettings.get().current_usd_rate)


def create_customs_order(*, number: int, lines: list[dict], by=None, fx_rate=None) -> CustomsOrder:
    if not number or int(number) <= 0:
        raise CustomsOrderError("Номер заказа должен быть положительным числом.")
    rate = Decimal(fx_rate if fx_rate is not None else current_fx_rate())
    with transaction.atomic():
        if CustomsOrder.objects.filter(number=number).exists():
            raise CustomsOrderError("Заказ с таким номером уже существует.")
        if not lines:
            raise CustomsOrderError("Выберите хотя бы одну позицию.")
        order = CustomsOrder.objects.create(number=number, created_by=by, fx_rate=rate)
        total_qty = Decimal("0")
        total_rub = Decimal("0")
        for line in lines:
            usd = line.get("usd_price")
            if usd is None:
                article = line.get("number") or "без артикула"
                raise CustomsOrderError(f"Нет оптовой цены для артикула {article}.")
            qty = Decimal(line.get("quantity", 0))
            rub = (Decimal(usd) * qty * rate).quantize(Decimal("0.01"))
            source = line.get("source") or (
                "ordered" if line.get("provenance") == "ordered" else line.get("kind")
            )
            source_id = line.get("source_id") or line.get("line_id")
            if CustomsOrderLine.objects.filter(source=source, source_id=source_id).exists():
                raise CustomsOrderError("Одна из позиций уже входит в другой таможенный заказ.")
            CustomsOrderLine.objects.create(
                order=order, source=source, source_id=source_id,
                article=line.get("number", ""), name_ru=line.get("name_ru", ""),
                name_en=line.get("name_en", ""), manufacturer=line.get("manufacturer", ""),
                quantity=qty, wholesale_usd=Decimal(usd), rub_amount=rub,
                is_analog=bool(line.get("is_analog")), is_ordered=source == "ordered",
            )
            total_qty += qty
            total_rub += rub
        order.total_quantity = total_qty
        order.total_rub = total_rub
        order.save(update_fields=["total_quantity", "total_rub"])
        return order


def eligible_customs_sources() -> list[dict]:
    """Stable, line-level unassigned canonical customs dataset."""
    from apps.actions.customs_history import _customs_rows_from_lines, canonical_customs_lines

    lines = [line for line in canonical_customs_lines() if line["quantity"] > 0]
    memberships = set(CustomsOrderLine.objects.values_list("source", "source_id"))
    rows = _customs_rows_from_lines(lines)
    by_key = {row["source_key"]: row for row in rows}
    result = []
    for line in lines:
        source = "sale" if line["kind"] == "sale" else "repair"
        marker = (source, line["line_id"])
        if marker in memberships:
            continue
        version = line["version"]
        key = (line["part_id"], version.pk if version is not None else None, line["number"])
        row = by_key.get(key)
        if row is None:
            continue
        result.append({
            "source": source, "source_id": line["line_id"], "occurred_at": line["occurred_at"],
            "number": line["number"], "quantity": line["quantity"],
            "is_analog": bool(line.get("is_analog")),
            "usd_price": row["usd_price"], "name_ru": row["name_ru"],
            "name_en": row["name_en"], "manufacturer": row["manufacturer"],
        })
    return sorted(result, key=lambda x: (x["occurred_at"] or "", x["source"], x["source_id"]))


@transaction.atomic
def create_customs_order_from_boundary(*, number: int, boundary_source: tuple[str, int], by=None):
    candidates = eligible_customs_sources()
    markers = [(x["source"], x["source_id"]) for x in candidates]
    if boundary_source not in markers:
        raise CustomsOrderError("Состав изменился. Обновите список и повторите.")
    selected = candidates[: markers.index(boundary_source) + 1]
    return create_customs_order(number=number, lines=selected, by=by)
