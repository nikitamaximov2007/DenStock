"""Layer 32 — сервисы пересчёта ячейки. Единственная точка мутаций сессии.

Скан и черновик сессии склад НЕ трогают. Остаток пишется только при проведении,
через существующий receipts.post_receipt (по адресу сессии). Автосоздание
складских карточек из BRP выполняется при конвертации/проведении, чтобы
пользователь только пикал, а не жал «Создать карточку» на каждую позицию.
"""
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.brp.models import BrpCatalogPart
from apps.brp.pricing import current_customer_price_rub
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import PartBarcode, PartNumber, PartType, normalize_number
from apps.receipts.models import Receipt
from apps.receipts.services import add_line, create_receipt, post_receipt
from apps.suppliers.models import Supplier

from .models import InventoryCountingLine, InventoryCountingSession, InventoryScanEvent

INTAKE_SUPPLIER_NAME = "Стартовый ввод"
UNKNOWN_NAME = "Неизвестная деталь"


class CountingError(Exception):
    """Нарушение правил сессии пересчёта."""


def _ensure_draft(session: InventoryCountingSession) -> None:
    if session.status != InventoryCountingSession.Status.DRAFT:
        raise CountingError("Сессия уже завершена: сканирование недоступно.")


def start_session(*, location, comment="", by=None) -> InventoryCountingSession:
    return InventoryCountingSession.objects.create(
        storage_location=location,
        full_address=location.code,
        title=f"Инвентаризация {location.code}",
        comment=(comment or "").strip(),
        created_by=by,
    )


# --- Сопоставление скана (склад имеет приоритет над BRP) --------------------------


def find_brp_by_number(norm: str) -> BrpCatalogPart | None:
    """Позиция BRP по нормализованному номеру (material_no и обе замены).

    Приоритет (hotfix 32.3.1): ТОЧНОЕ совпадение material_no ВСЕГДА выше
    совпадения по замене номера. Реальный кейс: у 417224458 (розница 0,
    статус USE) замена 417224916; при скане 417224916 должна выбираться
    сама позиция 417224916 с настоящей ценой, а не старый номер по замене.
    Внутри группы предпочитается ненулевая розница, затем меньший pk:
    выбор детерминирован.
    """
    if not norm:
        return None
    from django.db.models import Case, IntegerField, Value, When

    return (
        BrpCatalogPart.objects.filter(
            Q(material_no_norm=norm)
            | Q(replacement_no_1_norm=norm)
            | Q(replacement_no_2_norm=norm)
        )
        .order_by(
            Case(
                When(material_no_norm=norm, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            Case(
                When(retail_price_usd__gt=0, then=Value(0)),
                default=Value(1),
                output_field=IntegerField(),
            ),
            "pk",
        )
        .first()
    )


def find_brp_price_source(
    norm: str, selected: BrpCatalogPart | None
) -> BrpCatalogPart | None:
    """ИСТОЧНИК ЦЕНЫ для номера: сама позиция или связанная замена с ценой.

    Личность строки и источник цены разделены (hotfix 32.3.2): строка
    остаётся привязанной к точному номеру, но если у него розница 0, цена
    берётся из связанной по цепочке замен позиции с розницей > 0. Реальный
    кейс: у 250000059 розница 0, а у 250000418 (замена указывает на
    250000059) розница 4.19 $ -> 616 ₽.

    Кандидаты: позиции, связанные с отсканированным номером (material_no
    или замены = номеру), плюс позиции, на которые ссылаются замены самой
    выбранной позиции. Порядок детерминирован: розница > 0, меньший pk.
    Если ни у кого розницы нет, источником остаётся выбранная позиция (0).
    """
    if selected is not None and (
        selected.retail_price_usd is not None and selected.retail_price_usd > 0
    ):
        return selected
    if not norm and selected is None:
        return None
    related = Q()
    if norm:
        related |= (
            Q(material_no_norm=norm)
            | Q(replacement_no_1_norm=norm)
            | Q(replacement_no_2_norm=norm)
        )
    if selected is not None:
        for repl_norm in (selected.replacement_no_1_norm, selected.replacement_no_2_norm):
            if repl_norm:
                related |= Q(material_no_norm=repl_norm)
    if not related:
        return selected
    priced = (
        BrpCatalogPart.objects.filter(related, retail_price_usd__gt=0)
        .order_by("pk")
        .first()
    )
    return priced or selected


def _effective_brp_price(norm: str, brp: BrpCatalogPart):
    """Цена клиента для строки: от источника цены (целые рубли, Decimal)."""
    price_source = find_brp_price_source(norm, brp)
    if price_source is None:
        return None
    return current_customer_price_rub(price_source.retail_price_usd)


def _match(norm: str, raw: str):
    """Найти совпадение: (source, warehouse_part, brp_part, display_name, price)."""
    part = None
    if norm:
        part_id = (
            PartNumber.objects.filter(normalized_value=norm)
            .values_list("part_id", flat=True)
            .first()
        )
        if part_id is None and raw:
            part_id = (
                PartBarcode.objects.filter(value__iexact=raw)
                .values_list("part_id", flat=True)
                .first()
            )
        if part_id is not None:
            part = PartType.objects.filter(pk=part_id).first()
    if part is not None:
        return ("warehouse", part, None, part.name, part.recommended_price)

    brp = find_brp_by_number(norm)
    if brp is not None:
        price = _effective_brp_price(norm, brp)
        return ("brp_catalog", None, brp, brp.part_desc or brp.material_no, price)

    return ("unknown", None, None, UNKNOWN_NAME, None)


@transaction.atomic
def record_scan(
    session: InventoryCountingSession, raw_value: str, *, by=None
) -> InventoryCountingLine:
    """Записать один скан: сырое событие + инкремент сгруппированной строки."""
    _ensure_draft(session)
    raw = (raw_value or "").strip()
    if not raw:
        raise CountingError("Пустой скан.")
    norm = normalize_number(raw) or raw.upper()

    line = (
        InventoryCountingLine.objects.select_for_update()
        .filter(session=session, normalized_value=norm)
        .first()
    )
    if line is None:
        source, part, brp, display, price = _match(norm, raw)
        line = InventoryCountingLine.objects.create(
            session=session,
            scanned_value=raw,
            normalized_value=norm,
            warehouse_part=part,
            brp_catalog_part=brp,
            display_name=display[:255],
            source=source,
            quantity_counted=Decimal("1"),
            scan_count=1,
            final_customer_price_rub=price,
            last_scanned_at=timezone.now(),
        )
    else:
        line.quantity_counted = line.quantity_counted + 1
        line.scan_count = line.scan_count + 1
        line.last_scanned_at = timezone.now()
        line.save(update_fields=["quantity_counted", "scan_count", "last_scanned_at"])

    InventoryScanEvent.objects.create(
        session=session, raw_value=raw, normalized_value=norm,
        matched_line=line, created_by=by,
    )
    return line


@transaction.atomic
def undo_last_scan(session: InventoryCountingSession) -> bool:
    """Отменить последний скан: минус 1 к строке; строку с нулём удалить."""
    _ensure_draft(session)
    event = (
        InventoryScanEvent.objects.select_for_update()
        .filter(session=session, is_reverted=False)
        .order_by("-id")
        .first()
    )
    if event is None:
        return False
    event.is_reverted = True
    event.save(update_fields=["is_reverted"])
    line = event.matched_line
    if line is not None:
        line.quantity_counted = line.quantity_counted - 1
        line.scan_count = max(0, line.scan_count - 1)
        if line.quantity_counted <= 0:
            line.delete()
        else:
            line.save(update_fields=["quantity_counted", "scan_count"])
    return True


def set_line_quantity(line: InventoryCountingLine, quantity) -> None:
    _ensure_draft(line.session)
    try:
        quantity = Decimal(str(quantity))
    except (InvalidOperation, TypeError) as exc:
        raise CountingError("Некорректное количество.") from exc
    if quantity <= 0:
        line.delete()
        return
    line.quantity_counted = quantity
    line.save(update_fields=["quantity_counted"])


def remove_line(line: InventoryCountingLine) -> None:
    _ensure_draft(line.session)
    line.delete()


def refresh_draft_prices(session: InventoryCountingSession) -> int:
    """Освежить BRP-строки ЧЕРНОВИКА: перепривязка и цены по текущему каталогу.

    Зачем: после починки каталога (реимпорт, приоритет точного номера) уже
    отпиканная ячейка должна показать правильные позиции и цены БЕЗ
    повторного сканирования. Для каждой строки с привязкой к BRP номер
    строки заново прогоняется через find_brp_by_number (hotfix 32.3.1):
    если лучшая позиция изменилась (например, строка была привязана к
    старому номеру по замене, а точный номер существует), строка
    перепривязывается, название и цена обновляются. Количество и число
    сканов НЕ меняются.

    Только черновики: сконвертированные и проведённые сессии, документы,
    движения и остатки не трогаются. Возвращает число обновлённых строк.
    """
    # Статус берём из базы: переданный объект может быть устаревшим
    # (например, после post_session), а снимки истории трогать нельзя.
    current_status = (
        InventoryCountingSession.objects.filter(pk=session.pk)
        .values_list("status", flat=True)
        .first()
    )
    if current_status != InventoryCountingSession.Status.DRAFT:
        return 0
    changed = []
    lines = session.lines.filter(brp_catalog_part__isnull=False).select_related(
        "brp_catalog_part"
    )
    for line in lines:
        best = find_brp_by_number(line.normalized_value) or line.brp_catalog_part
        price = _effective_brp_price(line.normalized_value, best)
        dirty = False
        if best.pk != line.brp_catalog_part_id:
            line.brp_catalog_part = best
            line.display_name = (best.part_desc or best.material_no)[:255]
            line.source = InventoryCountingLine.Source.BRP
            dirty = True
        if price != line.final_customer_price_rub:
            line.final_customer_price_rub = price
            dirty = True
        if dirty:
            changed.append(line)
    if changed:
        InventoryCountingLine.objects.bulk_update(
            changed,
            ["brp_catalog_part", "display_name", "source", "final_customer_price_rub"],
        )
    return len(changed)


def resolve_unknown_to_brp(line: InventoryCountingLine, brp_part: BrpCatalogPart) -> None:
    _ensure_draft(line.session)
    line.brp_catalog_part = brp_part
    line.warehouse_part = None
    line.source = InventoryCountingLine.Source.BRP
    line.display_name = (brp_part.part_desc or brp_part.material_no)[:255]
    line.final_customer_price_rub = _effective_brp_price(
        brp_part.material_no_norm, brp_part
    )
    line.save()


# Режимы сортировки разбора стоимости (Layer 32.4.1). Ключ -> подпись в UI.
# По умолчанию sum_desc: разбор нужен прежде всего чтобы понять, что даёт
# основной вклад в стоимость ячейки.
VALUE_SORTS = {
    "sum_desc": "По сумме: сначала дорогие",
    "sum_asc": "По сумме: сначала дешёвые",
    "qty_desc": "По количеству: больше сначала",
    "qty_asc": "По количеству: меньше сначала",
    "price_desc": "По цене: дороже сначала",
    "price_asc": "По цене: дешевле сначала",
    "number_asc": "По номеру: А → Я",
    "number_desc": "По номеру: Я → А",
    "original": "Как в инвентаризации",
    "original_desc": "Обратный порядок",
}
DEFAULT_VALUE_SORT = "sum_desc"

# Составные ключи сортировки: Decimal с минусом = по убыванию; последний
# компонент у денежно-количественных режимов: номер по возрастанию, поэтому
# порядок детерминирован и стабилен.
_VALUE_SORT_KEYS = {
    "sum_desc": lambda r: (
        -r["line_total_rub"], -r["customer_price_rub"], -r["quantity"], r["normalized"],
    ),
    "sum_asc": lambda r: (
        r["line_total_rub"], r["customer_price_rub"], r["quantity"], r["normalized"],
    ),
    "qty_desc": lambda r: (-r["quantity"], -r["line_total_rub"], r["normalized"]),
    "qty_asc": lambda r: (r["quantity"], -r["line_total_rub"], r["normalized"]),
    "price_desc": lambda r: (-r["customer_price_rub"], -r["line_total_rub"], r["normalized"]),
    "price_asc": lambda r: (r["customer_price_rub"], -r["line_total_rub"], r["normalized"]),
    "number_asc": lambda r: (r["normalized"],),
}


def _sort_breakdown_rows(rows: list[dict], sort: str) -> list[dict]:
    if sort == "original":
        return rows
    if sort == "original_desc":
        return list(reversed(rows))
    if sort == "number_desc":
        return sorted(rows, key=lambda r: r["normalized"], reverse=True)
    return sorted(rows, key=_VALUE_SORT_KEYS[sort])


def get_session_value_breakdown(
    session: InventoryCountingSession, sort: str = DEFAULT_VALUE_SORT
) -> dict:
    """Разбор «Стоимости ячейки»: строка за строкой, количество x цена = сумма.

    Единая точка для модального окна и тестов. Только Decimal; сумма строки
    равна quantity_counted * final_customer_price_rub (цены уже в целых
    рублях); строки без цены участвуют с нулём и НЕ скрываются. Исходный
    порядок строк («Как в инвентаризации») совпадает с таблицей пересчёта
    (Meta.ordering модели); sort меняет только порядок в разборе, итоги
    от сортировки не зависят. Неизвестный sort откатывается к sum_desc.
    """
    if sort not in VALUE_SORTS:
        sort = DEFAULT_VALUE_SORT
    rows = []
    total_quantity = Decimal("0")
    total_value = Decimal("0")
    for line in session.lines.all():
        price = line.final_customer_price_rub
        line_total = line.quantity_counted * price if price is not None else Decimal("0")
        if line.needs_review:
            source_label = "Требует разбора"
        else:
            source_label = line.get_source_display()
        rows.append({
            "number": line.scanned_value,
            "normalized": line.normalized_value,
            "name": line.display_name,
            "source_label": source_label,
            "quantity": line.quantity_counted,
            "customer_price_rub": price if price is not None else Decimal("0"),
            "line_total_rub": line_total,
        })
        total_quantity += line.quantity_counted
        total_value += line_total
    return {
        "rows": _sort_breakdown_rows(rows, sort),
        "positions_count": len(rows),
        "total_quantity": total_quantity,
        "total_value_rub": total_value,
        "sort": sort,
    }


def resolve_unknown_to_part(line: InventoryCountingLine, part: PartType) -> None:
    _ensure_draft(line.session)
    line.warehouse_part = part
    line.brp_catalog_part = None
    line.source = InventoryCountingLine.Source.WAREHOUSE
    line.display_name = part.name[:255]
    line.final_customer_price_rub = part.recommended_price
    line.save()


# --- Конвертация в документ и проведение ------------------------------------------


def _intake_supplier() -> Supplier:
    supplier, _ = Supplier.objects.get_or_create(
        name=INTAKE_SUPPLIER_NAME, defaults={"is_active": True}
    )
    return supplier


@transaction.atomic
def convert_to_receipt(
    session: InventoryCountingSession, *, by=None, unit_cost=Decimal("0")
) -> Receipt:
    """Создать черновик документа из сессии. Автосоздаёт карточки из BRP.

    Склад НЕ меняется (это черновик поступления). Неизвестные строки блокируют
    конвертацию: их надо разобрать или удалить. Идемпотентно: если документ уже
    создан, возвращает его.
    """
    if session.status == InventoryCountingSession.Status.CONVERTED and session.converted_receipt:
        return session.converted_receipt
    if session.status != InventoryCountingSession.Status.DRAFT:
        raise CountingError("Сессия уже проведена или отменена.")
    # Перед конвертацией снимки строк освежаются (правильная привязка и
    # эффективная цена по 32.3.1/32.3.2): документ и карточки получают
    # ровно те цены, которые пользователь видел в пересчёте.
    refresh_draft_prices(session)
    lines = list(session.lines.select_related("warehouse_part", "brp_catalog_part"))
    if not lines:
        raise CountingError("Нельзя создать документ из пустой сессии.")
    unknown = [line for line in lines if line.source == InventoryCountingLine.Source.UNKNOWN]
    if unknown:
        raise CountingError(
            f"Есть неразобранные позиции ({len(unknown)}): привяжите их к складу или "
            "BRP-каталогу, либо удалите строки."
        )
    try:
        unit_cost = Decimal(str(unit_cost))
    except (InvalidOperation, TypeError):
        unit_cost = Decimal("0")
    if unit_cost < 0:
        raise CountingError("Себестоимость не может быть отрицательной.")

    receipt = create_receipt(
        supplier=_intake_supplier(),
        comment=f"Инвентаризация ячейки {session.full_address}",
        by=by,
    )
    for line in lines:
        part = line.warehouse_part
        if part is None and line.brp_catalog_part is not None:
            # Автосоздание карточки из BRP (без ручного «Создать карточку»).
            # Личность карточки — отсканированная позиция; если у неё розница
            # 0, а в пересчёте показана эффективная цена от замены (32.3.2),
            # эта цена фиксируется в снимке (manual override), чтобы карточка
            # не получила 0 ₽ вопреки тому, что видел пользователь.
            identity_retail = line.brp_catalog_part.retail_price_usd
            effective = line.final_customer_price_rub
            manual = None
            if (identity_retail is None or identity_retail <= 0) and effective:
                manual = effective
            part = promote_to_warehouse(line.brp_catalog_part, by=by, manual_price=manual)
            line.warehouse_part = part
            line.source = InventoryCountingLine.Source.WAREHOUSE
            line.save(update_fields=["warehouse_part", "source"])
        if part is None:
            raise CountingError(f"Строку «{line.scanned_value}» не с чем связать.")
        add_line(
            receipt,
            part_type=part,
            quantity=line.quantity_counted,
            unit_cost_rub=unit_cost,
            location=session.storage_location,
            comment=f"Пересчёт {session.full_address}",
        )

    session.status = InventoryCountingSession.Status.CONVERTED
    session.converted_receipt = receipt
    session.save(update_fields=["status", "converted_receipt", "updated_at"])
    return receipt


@transaction.atomic
def post_session(session: InventoryCountingSession, *, by=None) -> Receipt:
    """Провести инвентаризацию: остаток пишется по адресу сессии.

    Защита от двойного проведения: сессия блокируется, повторно провести
    нельзя (иначе один и тот же пересчёт удвоил бы остаток).
    """
    session = InventoryCountingSession.objects.select_for_update().get(pk=session.pk)
    if session.status == InventoryCountingSession.Status.POSTED:
        raise CountingError("Эта сессия уже проведена: повторное проведение удвоило бы остаток.")
    if session.status == InventoryCountingSession.Status.CANCELLED:
        raise CountingError("Сессия отменена.")
    receipt = session.converted_receipt
    if receipt is None:
        receipt = convert_to_receipt(session, by=by)
        session.refresh_from_db()
    post_receipt(receipt, by=by)
    session.status = InventoryCountingSession.Status.POSTED
    session.posted_at = timezone.now()
    session.save(update_fields=["status", "posted_at", "updated_at"])
    return receipt


def cancel_session(session: InventoryCountingSession) -> None:
    if session.status == InventoryCountingSession.Status.POSTED:
        raise CountingError("Проведённую сессию отменить нельзя.")
    session.status = InventoryCountingSession.Status.CANCELLED
    session.save(update_fields=["status", "updated_at"])


# --- Удаление черновика (hotfix 32.2) ----------------------------------------------

CANNOT_DELETE_MESSAGE = (
    "Эту инвентаризацию удалить нельзя, потому что она уже завершена "
    "или связана с документом склада."
)


def can_delete_session(session: InventoryCountingSession) -> bool:
    """Удалять можно ТОЛЬКО незавершённый черновик, не связанный с документом.

    После «Завершить пересчёт» (конвертация/проведение) сессия — часть
    истории склада и не удаляется. Отменённые сессии тоже остаются в истории.
    """
    return (
        session.status == InventoryCountingSession.Status.DRAFT
        and session.converted_receipt_id is None
    )


@transaction.atomic
def delete_session(session: InventoryCountingSession) -> str:
    """Удалить черновик сессии вместе со сканами и строками. Склад не трогает.

    Сканы и строки удаляются каскадом (FK on_delete=CASCADE); документ
    поступления, движения, остатки и StorageLocation не затрагиваются в
    принципе: у черновика нет документа, а остальные связи защищены.
    Возвращает адрес удалённой сессии для сообщения пользователю.
    """
    session = InventoryCountingSession.objects.select_for_update().get(pk=session.pk)
    if not can_delete_session(session):
        raise CountingError(CANNOT_DELETE_MESSAGE)
    address = session.full_address
    session.delete()
    return address
