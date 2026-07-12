"""Read-only helpers for human-friendly inventory presentation.

Единая (canonical) точка exact identity детали для всего UI:
BRP material_no -> Polaris part_number -> primary складской номер ->
не-analog складской номер -> «Артикул не указан». Аналог, replacement,
superseded и источник цены identity НЕ являются никогда; `.numbers.first()`
запрещён (PartNumber.Meta.ordering ставит analog раньше oem).

Чтобы не плодить N+1 на списках, identity готовится заранее:
`with_part_identity()` для queryset'ов, `attach_part_identity()` для готовых
строк, `identity_numbers_prefetch()` для точечного prefetch.
"""

from django import forms
from django.core.exceptions import ObjectDoesNotExist
from django.db.models import Prefetch, Q

from apps.catalog.models import PartNumber

NO_EXACT_NUMBER = "Артикул не указан"


def identity_numbers_prefetch(part_field: str = "part_type") -> Prefetch:
    """Prefetch exact/primary warehouse numbers without analog ordering traps.

    `part_field` — путь до PartType от префетчиваемого объекта
    ("part_type" для лотов/строк, "" для queryset самих PartType).
    """
    lookup = f"{part_field}__numbers" if part_field else "numbers"
    return Prefetch(
        lookup,
        queryset=(
            PartNumber.objects.exclude(kind=PartNumber.Kind.ANALOG)
            .order_by("-is_primary", "pk")
        ),
        to_attr="identity_numbers_for_display",
    )


def analog_numbers_prefetch(part_field: str = "") -> Prefetch:
    """Prefetch ТОЛЬКО аналогов — показывать отдельно и явно подписанными."""
    lookup = f"{part_field}__numbers" if part_field else "numbers"
    return Prefetch(
        lookup,
        queryset=PartNumber.objects.filter(kind=PartNumber.Kind.ANALOG).order_by("value"),
        to_attr="analog_numbers_for_display",
    )


def with_part_identity(queryset, part_field: str = "part_type"):
    """select_related/prefetch, достаточные для part_exact_number без N+1."""
    prefix = f"{part_field}__" if part_field else ""
    return queryset.select_related(
        f"{prefix}brp_link__brp_part",
        f"{prefix}polaris_link__polaris_part",
        f"{prefix}manufacturer",
    ).prefetch_related(identity_numbers_prefetch(part_field))


def part_exact_number(part, default: str = NO_EXACT_NUMBER) -> str:
    """Return catalog identity, never replacement/analog/price-source number."""
    try:
        return part.brp_link.brp_part.material_no
    except (AttributeError, ObjectDoesNotExist):
        pass
    try:
        return part.polaris_link.polaris_part.part_number
    except (AttributeError, ObjectDoesNotExist):
        pass

    numbers = getattr(part, "identity_numbers_for_display", None)
    if numbers is None:
        numbers = list(
            PartNumber.objects.filter(part=part)
            .exclude(kind=PartNumber.Kind.ANALOG)
            .order_by("-is_primary", "pk")[:1]
        )
    return numbers[0].value if numbers else default


def manufacturer_display(part) -> str:
    """Производитель для UI: каталог BRP/Polaris сильнее справочника карточки."""
    try:
        if part.brp_link is not None:
            return "BRP"
    except (AttributeError, ObjectDoesNotExist):
        pass
    try:
        if part.polaris_link is not None:
            return "POLARIS"
    except (AttributeError, ObjectDoesNotExist):
        pass
    return str(part.manufacturer) if part.manufacturer_id else ""


def attach_part_identity(rows, part_attr: str = "part_type") -> None:
    """Приложить к строкам exact-артикул и производителя их детали.

    Строки — любые объекты с атрибутом `part_attr` (лот, строка документа,
    aggregate-строка отчёта). Queryset должен быть подготовлен через
    with_part_identity(), иначе на каждой строке будут запросы.
    """
    for row in rows:
        part = getattr(row, part_attr, None) if part_attr else row
        if part is None:
            row.part_exact_number = ""
            row.part_manufacturer = ""
            continue
        row.part_exact_number = part_exact_number(part, default="")
        row.part_manufacturer = manufacturer_display(part)


def _quantity_text(value) -> str:
    """Кол-во без хвостовых нулей: 3.000 -> '3', 2.500 -> '2.5'."""
    return f"{value.normalize():f}"


def part_option_label(part) -> str:
    """Подпись опции выбора детали: название · артикул [· производитель]."""
    pieces = [part.name, part_exact_number(part)]
    manufacturer = manufacturer_display(part)
    if manufacturer:
        pieces.append(manufacturer)
    return " · ".join(pieces)


def lot_option_label(lot) -> str:
    """Подпись опции выбора лота: деталь · артикул [· произв.] · кол-во · ячейка."""
    pieces = [lot.part_type.name, part_exact_number(lot.part_type)]
    manufacturer = manufacturer_display(lot.part_type)
    if manufacturer:
        pieces.append(manufacturer)
    pieces.append(f"{_quantity_text(lot.quantity)} шт.")
    pieces.append(lot.location.code)
    return " · ".join(pieces)


def _plural_ru(count: int, one: str, few: str, many: str) -> str:
    tail, tail2 = count % 10, count % 100
    if tail == 1 and tail2 != 11:
        return one
    if tail in (2, 3, 4) and tail2 not in (12, 13, 14):
        return few
    return many


def lines_with_identity_prefetch(line_model, lines_attr: str = "lines") -> Prefetch:
    """Prefetch строк документа с identity деталей — для списков документов."""
    return Prefetch(
        lines_attr,
        queryset=with_part_identity(line_model.objects.select_related("part_type")),
    )


def attach_document_composition(documents, lines_attr: str = "lines") -> None:
    """Краткий состав документа для списка: первая позиция + «ещё N позиций».

    Документы должны быть получены с lines_with_identity_prefetch(), иначе
    каждая строка списка будет ходить в базу. Прикладывает:
    first_part_name / first_part_number / first_part_id / first_qty,
    more_lines_label («ещё 2 позиции · всего 3») и lines_total.
    """
    for doc in documents:
        lines = list(getattr(doc, lines_attr).all())
        doc.lines_total = len(lines)
        doc.first_part_name = ""
        doc.first_part_number = ""
        doc.first_part_id = None
        doc.first_qty = None
        doc.more_lines_label = ""
        if not lines:
            continue
        first = lines[0]
        doc.first_part_name = first.part_type.name
        doc.first_part_number = part_exact_number(first.part_type, default="")
        doc.first_part_id = first.part_type_id
        doc.first_qty = first.quantity
        more = len(lines) - 1
        if more:
            word = _plural_ru(more, "позиция", "позиции", "позиций")
            doc.more_lines_label = f"ещё {more} {word} · всего {len(lines)}"


class ExactPartChoiceField(forms.ModelChoiceField):
    """Select детали: видимая подпись — название + exact-артикул, value = pk.

    Queryset в форме оборачивать with_part_identity(..., part_field=""),
    иначе подпись каждой опции будет ходить в базу.
    """

    def label_from_instance(self, obj) -> str:
        return part_option_label(obj)


class ExactLotChoiceField(forms.ModelChoiceField):
    """Select лота: деталь идентифицируется названием и exact-артикулом.

    Queryset в форме оборачивать with_part_identity(...).
    """

    def label_from_instance(self, obj) -> str:
        return lot_option_label(obj)


def attach_movement_identity(movements) -> None:
    """Attach exact display snapshots to movements in one action query.

    Scanner sales/repairs and cancellation returns use WarehouseAction's
    immutable part_number snapshot. Other movements fall back to the exact
    catalog identity of their PartType.
    """
    movements = list(movements)
    sale_ids = {
        movement.document_id
        for movement in movements
        if movement.document_id
        and movement.document_type in {"sale", "stock_return"}
    }
    repair_ids = {
        movement.document_id
        for movement in movements
        if movement.document_id and movement.document_type == "repair_order"
    }
    snapshots = {}
    if sale_ids or repair_ids:
        from apps.actions.models import WarehouseAction

        actions = WarehouseAction.objects.filter(
            Q(sale_id__in=sale_ids) | Q(repair_order_id__in=repair_ids)
        ).order_by("pk")
        for action in actions:
            if action.sale_id:
                snapshots[("sale", action.sale_id)] = action.part_number
                snapshots[("stock_return", action.sale_id)] = action.part_number
            if action.repair_order_id:
                snapshots[("repair_order", action.repair_order_id)] = action.part_number

    for movement in movements:
        snapshot = snapshots.get((movement.document_type, movement.document_id))
        movement.display_part_number = snapshot or part_exact_number(movement.part_type)
