"""Каноническая история товарных строк для таможенной выгрузки.

До этого модуля выгрузка строилась по складскому журналу (``StockMovement``) и
называла деталь только тем номером, который сохранил сканер. У продажи или
ремонта, оформленных обычным документом, снимка сканера нет вовсе, поэтому
каждая такая строка получала статус «происхождение не доказано»: она не
попадала в Excel и одновременно блокировала выгрузку целиком. Списание же не
могло попасть в Excel никогда - его движение помечено ``write_off``, а такого
документа сканер не создаёт.

Отсюда и расхождение, которое видели раньше: в Excel уходила только
сканерная часть истории, а «Продажи и ремонты» показывали её целиком.

Теперь источник тот же, что у отчёта «Продажи и ремонты»:

* ``SaleLine`` проведённых продаж;
* ``RepairIssueLine`` проведённых ремонтов;

с тем же действующим количеством (выдано минус завершённые возвраты) и той же
клиентской суммой. Совпадение итогов становится свойством источника, а не
совпадением двух независимых расчётов.

Списания сюда намеренно не входят. Это не продажа и не ремонт: клиенту деталь
не уходила, в «Продажах и ремонтах» её нет, и включение списаний сделало бы
сверку неисполнимой. Ни одной строки Excel они и раньше не давали - только
блокировали выгрузку.

Артикул по-прежнему доказывается снимком сканера и ничем другим: сегодняшний
номер карточки историей не является. Но недоказанный артикул больше не
уничтожает строку - ячейка остаётся пустой, а сама операция уходит в Excel.
"""
import datetime
from decimal import Decimal

from django.db.models import Q

from apps.actions.customs_provenance import (
    ARTICLE_MISSING,
    ARTICLE_PROVEN,
    article_numbers_by_document,
)
from apps.actions.models import PartCustomsDataVersion, WarehouseAction
from apps.catalog.models import PartAnalog, PartNumber, PartType
from apps.core.part_lookup import normalize_number
from apps.inventory.models import StockMovement
from apps.procurement.models import money
from apps.repairs.models import RepairIssueLine, RepairOrder
from apps.sales.models import Sale, SaleLine

DEC0 = Decimal("0")
# Строка без даты документа сортируется первой, а не падает при сравнении с
# датой: у исторического документа поле проведения бывает пустым.
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)

SALE = "sale"
REPAIR = "repair"

# Какие канонические строки стоят за типом действия в фильтре отчёта. Резерв и
# возврат из ремонта товар клиенту не отдают, поэтому таможенного расхода за
# ними нет вовсе: пустой кортеж означает «строк нет», а не «фильтр не применён».
_ACTION_TYPE_KINDS = {
    WarehouseAction.Type.SALE: (SALE,),
    WarehouseAction.Type.REPAIR: (REPAIR,),
    WarehouseAction.Type.RESERVE: (),
    WarehouseAction.Type.REPAIR_RETURN: (),
}

_OUTBOUND_MOVEMENTS = (
    StockMovement.MovementType.SALE_ITEM, StockMovement.MovementType.SALE_LOT,
    StockMovement.MovementType.ISSUE_ITEM, StockMovement.MovementType.ISSUE_LOT,
)


def _part_ids_for_number(part_number: str) -> list[int]:
    return list(
        PartNumber.objects.filter(
            normalized_value=normalize_number(part_number)
        ).values_list("part_id", flat=True)
    )


def _sale_lines(*, date_from, date_to, part_number):
    """Строки проведённых продаж - тот же набор, что у «Продаж и ремонтов»."""
    lines = SaleLine.objects.filter(sale__status=Sale.Status.COMPLETED)
    if date_from:
        lines = lines.filter(sale__sold_at__date__gte=date_from)
    if date_to:
        lines = lines.filter(sale__sold_at__date__lte=date_to)
    if part_number:
        lines = lines.filter(
            Q(part_type_id__in=_part_ids_for_number(part_number))
            | Q(part_type__name__icontains=part_number)
        )
    return lines.select_related("sale", "part_type").order_by("sale__sold_at", "pk")


def _repair_lines(*, date_from, date_to, part_number):
    """Строки проведённых ремонтов - тот же набор, что у «Продаж и ремонтов»."""
    lines = RepairIssueLine.objects.filter(
        repair_order__status=RepairOrder.Status.COMPLETED
    )
    if date_from:
        lines = lines.filter(repair_order__completed_at__date__gte=date_from)
    if date_to:
        lines = lines.filter(repair_order__completed_at__date__lte=date_to)
    if part_number:
        lines = lines.filter(
            Q(part_type_id__in=_part_ids_for_number(part_number))
            | Q(part_type__name__icontains=part_number)
        )
    return lines.select_related("repair_order", "part_type").order_by(
        "repair_order__completed_at", "pk"
    )


def _matching_documents(q: str) -> set[tuple[str, int]]:
    """Документы, подходящие под поиск «Клиент / комментарий».

    Ищем и по снимку клиента в самом документе, и по комментарию сканерного
    действия: у документа, оформленного не сканером, комментария нет вовсе, а
    у сканерного клиент записан именно туда.
    """
    found: set[tuple[str, int]] = set()
    for pk in Sale.objects.filter(customer_name__icontains=q).values_list("pk", flat=True):
        found.add((SALE, pk))
    for pk in RepairOrder.objects.filter(
        customer_name__icontains=q
    ).values_list("pk", flat=True):
        found.add((REPAIR, pk))
    actions = WarehouseAction.objects.filter(customer_comment__icontains=q)
    for pk in actions.exclude(sale_id=None).values_list("sale_id", flat=True):
        found.add((SALE, pk))
    for pk in actions.exclude(repair_order_id=None).values_list(
        "repair_order_id", flat=True
    ):
        found.add((REPAIR, pk))
    return found


def _outbound_locations(sale_ids, repair_ids) -> dict[tuple, set[str]]:
    """Ячейки, из которых строки этих документов физически ушли со склада.

    Историческую ячейку хранит только складской журнал: у экземпляра поле
    «текущая ячейка» с тех пор могло измениться. Журнал здесь не источник
    строк, а указатель - им отвечает лишь фильтр по ячейке.
    """
    movements = StockMovement.objects.filter(
        movement_type__in=_OUTBOUND_MOVEMENTS
    ).filter(
        Q(document_type="sale", document_id__in=list(sale_ids))
        | Q(document_type="repair_order", document_id__in=list(repair_ids))
    ).select_related("from_location")
    places: dict[tuple, set[str]] = {}
    for movement in movements:
        kind = SALE if movement.document_type == "sale" else REPAIR
        source = (
            ("lot", movement.stock_lot_id)
            if movement.stock_lot_id is not None
            else ("item", movement.part_item_id)
        )
        code = movement.from_location.code if movement.from_location_id else ""
        places.setdefault((kind, movement.document_id, source), set()).add(code)
    return places


def _line_source(line) -> tuple[str, int | None]:
    if line.stock_lot_id is not None:
        return ("lot", line.stock_lot_id)
    return ("item", line.part_item_id)


def _returned_by_line(sale_lines, repair_lines) -> tuple[dict, dict]:
    """Завершённые возвраты по строке-источнику - правило отчёта, без изменений."""
    from apps.repairs.services import repair_returned_quantities
    from apps.reports.services import sale_returned_quantities

    return (
        sale_returned_quantities(sale_lines),
        repair_returned_quantities(repair_lines),
    )


def _versions_by_part(part_ids) -> dict[int, list]:
    versions: dict[int, list] = {part_id: [] for part_id in part_ids}
    for version in PartCustomsDataVersion.objects.filter(
        part_type_id__in=part_ids
    ).order_by("part_type_id", "effective_from", "version"):
        versions[version.part_type_id].append(version)
    return versions


def version_at(versions: list, at):
    """Версия таможенных данных, действовавшая в момент выбытия.

    Первая версия намеренно покрывает и более ранние выбытия: карточку
    заполняют уже после того, как деталь появилась. Следующие версии более
    раннюю операцию никогда не переписывают.
    """
    if not versions:
        return None
    if at is None:
        return versions[0]
    effective = [version for version in versions if version.effective_from <= at]
    return effective[-1] if effective else versions[0]


def canonical_customs_lines(
    *, date_from=None, date_to=None, action_type="", q="", part_number="", location_code="",
) -> list[dict]:
    """Канонические товарные строки, ушедшие клиенту, с таможенным профилем.

    Одна запись на строку документа. Количество - действующее: выдано минус
    завершённые возвраты, ровно как в «Продажах и ремонтах». Клиентская сумма
    считается тем же правилом того же отчёта и нужна только для сверки: в
    таможенную форму рубли не попадают.
    """
    kinds = _ACTION_TYPE_KINDS.get(action_type, (SALE, REPAIR)) if action_type else (SALE, REPAIR)
    if not kinds:
        return []
    window = {"date_from": date_from, "date_to": date_to, "part_number": part_number}
    sale_lines = list(_sale_lines(**window)) if SALE in kinds else []
    repair_lines = list(_repair_lines(**window)) if REPAIR in kinds else []
    if q:
        documents = _matching_documents(q)
        sale_lines = [
            line for line in sale_lines if (SALE, line.sale_id) in documents
        ]
        repair_lines = [
            line for line in repair_lines if (REPAIR, line.repair_order_id) in documents
        ]
    if not sale_lines and not repair_lines:
        return []

    sale_ids = {line.sale_id for line in sale_lines}
    repair_ids = {line.repair_order_id for line in repair_lines}
    if location_code:
        places = _outbound_locations(sale_ids, repair_ids)
        needle = location_code.casefold()

        def _at_location(kind, document_id, line):
            codes = places.get((kind, document_id, _line_source(line)), set())
            return any(needle in code.casefold() for code in codes)

        sale_lines = [
            line for line in sale_lines if _at_location(SALE, line.sale_id, line)
        ]
        repair_lines = [
            line for line in repair_lines if _at_location(REPAIR, line.repair_order_id, line)
        ]
        if not sale_lines and not repair_lines:
            return []
        sale_ids = {line.sale_id for line in sale_lines}
        repair_ids = {line.repair_order_id for line in repair_lines}

    sale_returned, repair_returned = _returned_by_line(sale_lines, repair_lines)
    amounts, unpriced_orders = _repair_line_amounts(repair_lines)
    part_ids = {line.part_type_id for line in sale_lines} | {
        line.part_type_id for line in repair_lines
    }
    versions_by_part = _versions_by_part(part_ids)
    parts = {
        part.pk: part
        for part in PartType.objects.filter(pk__in=part_ids)
        .select_related("manufacturer")
        .prefetch_related("numbers")
    }
    numbers = article_numbers_by_document(sale_ids, repair_ids)

    analog_part_ids = set(PartAnalog.objects.values_list("analog_id", flat=True))
    original_part_ids = set(PartAnalog.objects.values_list("original_id", flat=True))
    records = []
    for line in sale_lines:
        remaining = max(line.quantity - (sale_returned.get(line.pk) or DEC0), DEC0)
        record = _record(
            kind=SALE, document_id=line.sale_id, document_number=line.sale.number,
            customer=line.sale.customer_name, line=line, parts=parts,
            versions_by_part=versions_by_part, numbers=numbers,
            occurred_at=line.sale.sold_at, issued=line.quantity,
            returned=sale_returned.get(line.pk) or DEC0, remaining=remaining,
            amount=money(line.unit_price * remaining), amount_known=True,
        )
        record["is_analog"] = _is_analog_part(
            parts[line.part_type_id], analog_part_ids, original_part_ids
        )
        records.append(record)
    for line in repair_lines:
        remaining = max(line.quantity - (repair_returned.get(line.pk) or DEC0), DEC0)
        # Отчёт считает клиентскую сумму по заказу целиком: одна строка без
        # исторической цены делает неизвестной сумму всего заказа. Сверка
        # обязана трактовать это так же, иначе разойдётся на ровном месте.
        known = line.repair_order_id not in unpriced_orders
        record = _record(
            kind=REPAIR, document_id=line.repair_order_id,
            document_number=line.repair_order.number,
            customer=line.repair_order.customer_name, line=line, parts=parts,
            versions_by_part=versions_by_part, numbers=numbers,
            occurred_at=line.repair_order.completed_at, issued=line.quantity,
            returned=repair_returned.get(line.pk) or DEC0, remaining=remaining,
            amount=amounts[line.pk] if known else None, amount_known=known,
        )
        record["is_analog"] = _is_analog_part(
            parts[line.part_type_id], analog_part_ids, original_part_ids
        )
        records.append(record)
    # Тип документа в ключе обязателен: у строки продажи и строки ремонта
    # нумерация своя, и без него две разные строки с одним id встали бы в
    # произвольном порядке.
    records.sort(
        key=lambda record: (
            record["occurred_at"] or _EPOCH, record["kind"], record["line_id"]
        )
    )
    return records


def _is_analog_part(part, analog_part_ids: set[int], original_part_ids: set[int]) -> bool:
    """Classify only by explicit relation or the approved SPI identity rule."""
    if part.pk in original_part_ids:
        return False
    if part.pk in analog_part_ids:
        return True
    return bool(part.manufacturer and part.manufacturer.name.strip().casefold() == "spi")


def _repair_line_amounts(repair_lines):
    """Клиентские суммы строк ремонта и заказы, сумма которых неизвестна.

    «Неизвестна» решается по ЗАКАЗУ ЦЕЛИКОМ, как и в отчёте: одна строка без
    исторической цены делает неизвестной сумму всего заказа. Поэтому смотреть
    надо на все строки заказа, а не только на попавшие под фильтр выгрузки -
    иначе отфильтрованный вид назвал бы заказ посчитанным, а отчёт нет.
    """
    from apps.repairs.services import repair_customer_line_amounts

    if not repair_lines:
        return {}, set()
    order_ids = {line.repair_order_id for line in repair_lines}
    siblings = list(RepairIssueLine.objects.filter(repair_order_id__in=order_ids).only(
        "id", "repair_order_id", "part_type_id", "quantity", "customer_unit_price_rub"
    ))
    amounts = repair_customer_line_amounts(siblings)
    unpriced = {
        line.repair_order_id for line in siblings if amounts[line.pk] is None
    }
    return amounts, unpriced


def _record(
    *, kind, document_id, document_number, customer, line, parts, versions_by_part,
    numbers, occurred_at, issued, returned, remaining, amount, amount_known,
):
    proven = numbers.get((kind, document_id, line.part_type_id), set())
    if len(proven) == 1:
        number, article_status = next(iter(proven)), ARTICLE_PROVEN
    else:
        # Ни одного снимка или сразу несколько - доказать нечего. Номер
        # остаётся пустым, но операция из выгрузки не исчезает.
        number, article_status = "", ARTICLE_MISSING
    return {
        "kind": kind,
        "document_type": "sale" if kind == SALE else "repair_order",
        "document_id": document_id,
        "document_number": document_number,
        "customer": customer,
        "line_id": line.pk,
        "part": parts[line.part_type_id],
        "part_id": line.part_type_id,
        "occurred_at": occurred_at,
        "issued_quantity": issued,
        "returned_quantity": returned,
        "quantity": remaining,
        "amount": amount,
        "amount_known": amount_known,
        "number": number,
        "article_status": article_status,
        "version": version_at(versions_by_part.get(line.part_type_id, []), occurred_at),
    }
