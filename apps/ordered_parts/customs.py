"""Заказанные детали как отдельный источник строк таможенной выгрузки.

Модуль намеренно отдельный. Обычная выгрузка живёт в `apps.actions`, её сейчас
переделывают, и вплетать сюда логику заказов означало бы драться за один и тот
же файл. Здесь только источник строк; общий экспортёр вызывает его одной
функцией и красит артикул.

Что важно в семантике:

* заказ оформляется на ОРИГИНАЛЬНУЮ деталь, поэтому его строки идут в ОБЫЧНУЮ
  таможенную выгрузку, а не в выгрузку аналогов;
* одна запись заказа - одна единица (V1 без поля количества);
* предоплата в таможню не уходит: цена берётся тем же каталожным резолвером,
  что и у обычных строк. Предоплата это деньги клиента за услугу, а не
  стоимость товара на границе;
* строки заказов НИКОГДА не сливаются со строками продаж и ремонтов того же
  артикула. Иначе сотрудник потерял бы, какая часть количества заказная, а
  зелёная пометка расползлась бы на чужой расход.
"""
from decimal import Decimal

from apps.catalog.models import PartNumber, PartType, normalize_number

from .models import OrderedPart

PROVENANCE = "ordered"
# Одна запись раздела - одна заказанная единица. Поля количества в V1 нет:
# пользователь его не просил, а придуманное поле потом дороже отсутствующего.
UNIT_QUANTITY = Decimal("1")


def _part_ids_for_number(part_number: str) -> list[int]:
    return list(
        PartNumber.objects.filter(
            normalized_value=normalize_number(part_number)
        ).values_list("part_id", flat=True)
    )


def ordered_parts_for_customs(
    *, date_from=None, date_to=None, q="", part_number="", **_ignored,
):
    """Заказы, попадающие под фильтры отчёта. Read-only.

    Незнакомые фильтры (ячейка, тип действия) сюда не применяются осознанно: у
    заказанной детали нет ни ячейки, ни складского действия. Молча возвращать
    пустой список тоже нельзя - заказ существует независимо от склада.
    """
    orders = OrderedPart.objects.select_related("customer", "part_type")
    if date_from:
        orders = orders.filter(created_at__date__gte=date_from)
    if date_to:
        orders = orders.filter(created_at__date__lte=date_to)
    if q:
        orders = orders.filter(customer__name__icontains=q)
    if part_number:
        from django.db.models import Q

        orders = orders.filter(
            Q(article__icontains=part_number)
            | Q(part_type_id__in=_part_ids_for_number(part_number))
            | Q(part_type__name__icontains=part_number)
        )
    return orders.order_by("created_at", "pk")


def ordered_parts_customs_lines(**filters) -> list[dict]:
    """Канонические строки расхода по заказам: одна запись - одна единица."""
    from apps.actions.customs_history import version_at
    from apps.actions.models import PartCustomsDataVersion

    orders = list(ordered_parts_for_customs(**filters))
    if not orders:
        return []
    part_ids = {order.part_type_id for order in orders}
    versions: dict[int, list] = {part_id: [] for part_id in part_ids}
    for version in PartCustomsDataVersion.objects.filter(
        part_type_id__in=part_ids
    ).order_by("part_type_id", "effective_from", "version"):
        versions[version.part_type_id].append(version)
    parts = {
        part.pk: part
        for part in PartType.objects.filter(pk__in=part_ids).prefetch_related("numbers")
    }
    return [
        {
            "provenance": PROVENANCE,
            "kind": PROVENANCE,
            "document_type": "ordered_part",
            "document_id": order.pk,
            "document_number": f"ЗАКАЗ-{order.pk}",
            "customer": order.customer.name,
            "line_id": order.pk,
            "part": parts[order.part_type_id],
            "part_id": order.part_type_id,
            "occurred_at": order.created_at,
            "issued_quantity": UNIT_QUANTITY,
            "returned_quantity": Decimal("0"),
            "quantity": UNIT_QUANTITY,
            # Предоплата НЕ клиентская сумма расхода и не таможенная цена: она
            # не участвует ни в одной сверке денег.
            "amount": None,
            "amount_known": False,
            "prepayment_rub": order.prepayment_rub,
            "number": order.article,
            "version": version_at(versions.get(order.part_type_id, []), order.created_at),
        }
        for order in orders
    ]


def ordered_parts_customs_rows(**filters) -> list[dict]:
    """Строки XLSX по заказам: та же свёртка и те же каталожные факты.

    Заказы одного артикула под одной версией таможенных данных складываются в
    одну строку - количество при этом сохраняется точно, а происхождение
    остаётся однозначно «заказ». Со строкой продажи того же артикула она не
    сливается никогда: у них разное происхождение.
    """
    from apps.actions.models import PartCustomsInfo
    from apps.actions.services import _customs_row_from_version

    lines = ordered_parts_customs_lines(**filters)
    if not lines:
        return []
    parts, versions, totals = {}, {}, {}
    for line in lines:
        version = line["version"]
        key = (line["part_id"], version.pk if version is not None else None, line["number"])
        parts[line["part_id"]] = line["part"]
        versions[key] = version
        totals[key] = totals.get(key, Decimal("0")) + line["quantity"]
    customs_by_part = {
        info.part_type_id: info
        for info in PartCustomsInfo.objects.filter(part_type_id__in=parts)
    }
    rows = []
    for key, quantity in totals.items():
        part_id, _version_pk, number = key
        row = _customs_row_from_version(
            parts[part_id], versions[key], quantity,
            customs=customs_by_part.get(part_id), number=number,
        )
        row["number"] = number
        row["provenance"] = PROVENANCE
        row["source_key"] = (PROVENANCE, *key)
        rows.append(row)
    return sorted(
        rows,
        key=lambda row: (row["number"], row["name_ru"], row["source_key"][1]),
    )


def ordered_parts_reconciliation(**filters) -> dict:
    """Сверка отдельной вселенной заказов: свёртка не теряет и не удваивает.

    С «Продажами и ремонтами» эти строки не сверяются и сверяться не должны:
    заказанная деталь клиенту ещё не выдавалась и в том отчёте её нет.
    """
    lines = ordered_parts_customs_lines(**filters)
    rows = ordered_parts_customs_rows(**filters)
    quantity = sum((line["quantity"] for line in lines), Decimal("0"))
    row_quantity = sum((row["quantity"] for row in rows), Decimal("0"))
    keys = {
        (line["part_id"], line["version"].pk if line["version"] is not None else None,
         line["number"])
        for line in lines
    }
    row_keys = {row["source_key"][1:] for row in rows}
    return {
        "lines": lines,
        "rows": rows,
        "totals": {
            "line_count": len(lines),
            "row_count": len(rows),
            "quantity": quantity,
            "row_quantity": row_quantity,
            "prepayment_total": sum(
                (line["prepayment_rub"] for line in lines), Decimal("0")
            ),
        },
        "silent": sorted(keys - row_keys),
        "extra": sorted(row_keys - keys),
        "delta_quantity": quantity - row_quantity,
    }
