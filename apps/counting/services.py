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

    if norm:
        brp = BrpCatalogPart.objects.filter(
            Q(material_no_norm=norm)
            | Q(replacement_no_1_norm=norm)
            | Q(replacement_no_2_norm=norm)
        ).first()
        if brp is not None:
            price = current_customer_price_rub(brp.retail_price_usd)
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


def resolve_unknown_to_brp(line: InventoryCountingLine, brp_part: BrpCatalogPart) -> None:
    _ensure_draft(line.session)
    line.brp_catalog_part = brp_part
    line.warehouse_part = None
    line.source = InventoryCountingLine.Source.BRP
    line.display_name = (brp_part.part_desc or brp_part.material_no)[:255]
    line.final_customer_price_rub = current_customer_price_rub(brp_part.retail_price_usd)
    line.save()


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
            part = promote_to_warehouse(line.brp_catalog_part, by=by)
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
