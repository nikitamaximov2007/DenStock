"""Layer 33 — сервисы быстрых действий со склада и таможенного экспорта.

Физику склада НЕ дублируем: продажа/резерв/ремонт проводятся существующими
сервисами apps.sales и apps.repairs (движения, остатки, брони — там).
Здесь: поиск остатков по скану, раскладка количества по лотам выбранной
ячейки (FIFO), журнальная запись WarehouseAction для единого отчёта и
Excel-экспорт «Формы для заказа» (openpyxl, шаблон в apps/actions/customs_template/:
рантайм-ассет должен лежать в пакете, docs/ исключён из Docker-образа).
"""
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.utils import timezone

from apps.actions.customs_history import canonical_customs_lines
from apps.actions.customs_provenance import ARTICLE_PROVEN
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import (
    PartNumber,
    PartType,
    VehicleType,
    normalize_number,
)
from apps.catalog_import.models import AftermarketCatalogPart
from apps.core.part_lookup import (
    MatchSource,
    clean_lookup_value,
    lookup_part_by_id,
    resolve_part_lookup,
)
from apps.counting.services import find_brp_price_source
from apps.inventory.models import PartItem, StockLot
from apps.inventory.presentation import (
    NO_EXACT_NUMBER,
    manufacturer_display,
    part_exact_number,
    with_part_identity,
)
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink
from apps.polaris.services import find_polaris_price_source
from apps.procurement.models import money
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.sales.models import Sale
from apps.sales.services import (
    activate_reservation,
    active_reserved_for_lot,
    active_reserved_for_lots,
    active_reserved_item_ids,
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
)

from .models import PartCustomsDataVersion, PartCustomsInfo, WarehouseAction

# Шаблон — РАНТАЙМ-АССЕТ и лежит внутри пакета приложения, а не в docs/:
# каталог docs/ исключён из Docker-образа (.dockerignore), поэтому шаблон
# оттуда не попадал в production и экспорт падал с FileNotFoundError.
# Путь берётся от модуля, а не от BASE_DIR: работает в любом окружении.
TEMPLATE_PATH = (
    Path(__file__).resolve().parent / "customs_template" / "supplier_order_template.xlsx"
)
TEMPLATE_SHEET = "Лист1"
TEMPLATE_DATA_START_ROW = 10  # строка 10 шаблона — пример, перезаписывается данными
TEMPLATE_DATA_COLUMNS = "ABCDEFGHIJKLM"
# Строки 1-9 — инструкции и шапка (не трогаем). Ниже 10-й строки шаблон
# заранее заполнен BRP/CANADA/СНЕГОХОД и формулами: это заготовка, а не
# данные. Перед заполнением товарный диапазон очищается по значениям.
TEMPLATE_DATA_END_ROW = 149  # 150-я строка шаблона — служебная (merged F150:H150)

SALES_REPAIRS_PROVENANCE = "sales_repairs"
ORDERED_PROVENANCE = "ordered"

# openpyxl запрещает управляющие символы; текст, начинающийся с этих символов,
# Excel исполняет как формулу (formula injection).
_EXCEL_FORMULA_PREFIXES = ("=", "+", "-", "@")
_EXCEL_MAX_TEXT = 32767

NOT_FOUND_MESSAGE = "Деталь не найдена в остатках склада."
MULTI_LOCATION_MESSAGE = "Деталь найдена в нескольких ячейках. Выберите, откуда списать."
NOT_ENOUGH_MESSAGE = "Недостаточно доступного остатка в выбранной ячейке."
IDENTITY_MISMATCH_MESSAGE = "Отсканированный номер не соответствует выбранной детали."


class ActionError(Exception):
    """Действие со склада невозможно (валидация/доступность)."""


# --- Цена продажи ------------------------------------------------------------
# Незаполненная цена и цена «ноль» - разные вещи. Раньше отсутствие цены
# молча превращалось в 0.00, и деталь уходила с прилавка бесплатно, а в отчёте
# продажа выглядела законной. Ноль остаётся допустимым только там, где его
# действительно проставили в карточке детали.

SALE_PRICE_NOT_SET = (
    "«{name}»: цена не задана. Укажите цену в карточке детали перед продажей."
)
SALE_PRICE_STALE_ZERO = (
    "«{name}»: строка добавлена, когда цена ещё не была задана, и осталась "
    "нулевой. Уберите её и добавьте деталь заново."
)


def check_sale_line_price(part, unit_price) -> None:
    """Не дать провести продажу по цене, которой никто не назначал.

    В быстрых действиях цену продажи руками не вводят: она приходит из карточки
    детали. Значит ноль в строке имеет право быть только тогда, когда в карточке
    стоит именно ноль. Ремонта это правило не касается: там цена клиента
    необязательна по своей природе и показывается прочерком.
    """
    canonical = part.recommended_price
    if unit_price is None or (unit_price == 0 and canonical is None):
        raise ActionError(SALE_PRICE_NOT_SET.format(name=part.name))
    if unit_price == 0 and canonical != 0:
        raise ActionError(SALE_PRICE_STALE_ZERO.format(name=part.name))


# --- Поиск детали и остатков по скану ----------------------------------------------


def resolve_part(raw: str) -> PartType | None:
    """Compatibility wrapper around the canonical warehouse lookup."""
    result = resolve_part_lookup(raw)
    return result.candidate.part if result.found else None


def identity_number(part: PartType, scanned_raw: str = "") -> str:
    """Точный номер личности детали для снимка действия.

    Если отсканированное значение совпадает с одним из номеров детали
    (в т.ч. номером-заменой) — возвращаем именно его: работник продал ровно
    этот номер. Иначе основной номер детали (is_primary, затем pk) — это
    OEM/material_no, НЕ аналог. НИКОГДА не берём соседний/replacement номер
    по сортировке (это и был баг: PartNumber.ordering ставит analog раньше
    oem, и `.numbers.first` отдавал замену).
    """
    norm = normalize_number(scanned_raw or "")
    if norm:
        matched = (
            PartNumber.objects.filter(
                part=part,
                normalized_value=norm,
                kind__in=(PartNumber.Kind.OEM, PartNumber.Kind.ARTICLE),
            )
            .order_by("-is_primary", "pk")
            .first()
        )
        if matched is not None:
            return matched.value
    return part_exact_number(part, default="")


def _price_source_number(part: PartType) -> str:
    """Catalog number that supplied price when it differs from identity."""
    brp = _brp_part_for(part)
    if brp is not None:
        if brp.wholesale_price_usd and brp.wholesale_price_usd > 0:
            return ""
        source = find_brp_price_source(brp.material_no_norm, brp)
        if source is not None and source.pk != brp.pk:
            return source.material_no
        return ""
    polaris = _polaris_part_for(part)
    if polaris is not None:
        if polaris.wholesale_price_usd and polaris.wholesale_price_usd > 0:
            return ""
        source = find_polaris_price_source(polaris.part_number_norm, polaris)
        if source is not None and source.pk != polaris.pk:
            return source.part_number
    return ""


def _manufacturer_snapshot(part: PartType) -> str:
    return manufacturer_display(part)


def _lot_available(lot: StockLot) -> Decimal:
    return lot.quantity - active_reserved_for_lot(lot)


def stock_overview(part: PartType) -> dict:
    """Остатки детали по ячейкам: физически / зарезервировано / доступно.

    Быстрые действия работают с количественными лотами; поштучные экземпляры
    показываются числом со ссылкой на существующий флоу карточки детали.
    """
    candidate = lookup_part_by_id(part, include_price=True)
    lots = list(
        StockLot.objects.filter(part_type=part, status=StockLot.Status.AVAILABLE)
        .select_related("location")
        .order_by("created_at", "pk")
    )
    by_location: dict[int, dict] = {}
    reserved_by_lot = active_reserved_for_lots(lots)
    for lot in lots:
        row = by_location.setdefault(
            lot.location_id,
            {
                "location": lot.location,
                "physical": Decimal("0"),
                "reserved": Decimal("0"),
                "available": Decimal("0"),
                "lots": [],
            },
        )
        reserved = reserved_by_lot.get(lot.pk, Decimal("0"))
        row["physical"] += lot.quantity
        row["reserved"] += reserved
        row["available"] += lot.quantity - reserved
        row["lots"].append(lot)
    locations = sorted(by_location.values(), key=lambda row: row["location"].code)
    unit_item_ids = list(
        PartItem.objects.filter(part_type=part, status=PartItem.Status.AVAILABLE)
        .values_list("pk", flat=True)
    )
    reserved_item_ids = active_reserved_item_ids(unit_item_ids)
    unit_items = len(unit_item_ids)
    unit_available = unit_items - len(reserved_item_ids)
    return {
        "part": candidate.part,
        "lookup": candidate,
        "locations": locations,
        # This is the same availability rule used by sale/repair completion:
        # available state minus active reservations. Quarantine is deliberately
        # excluded from an operator's sell/repair availability.
        "total_available": sum((row["available"] for row in locations), Decimal("0")),
        "unit_items": unit_items,
        "unit_available": unit_available,
    }


# --- Проведение действия -------------------------------------------------------------


def _split_quantity_over_lots(lots, quantity: Decimal):
    """Раскладка количества по лотам ячейки (FIFO). [(lot, portion), ...]."""
    portions = []
    remaining = quantity
    for lot in lots:
        if remaining <= 0:
            break
        available = _lot_available(lot)
        if available <= 0:
            continue
        portion = min(available, remaining)
        portions.append((lot, portion))
        remaining -= portion
    if remaining > 0:
        raise ActionError(NOT_ENOUGH_MESSAGE)
    return portions


def parse_quantity(value, *, allow_zero=False) -> Decimal:
    """Разобрать количество из формы (запятая как разделитель). По умолчанию > 0.

    `allow_zero` нужен корзине: ноль там означает «убрать позицию», а не ошибку.
    """
    try:
        quantity = Decimal(str(value).replace(",", "."))
    except (InvalidOperation, TypeError) as exc:
        raise ActionError("Некорректное количество.") from exc
    if quantity < 0 or (quantity == 0 and not allow_zero):
        raise ActionError("Количество должно быть больше нуля.")
    return quantity


def _request_token(value) -> str | None:
    token = str(value or "").strip()
    if len(token) > 64:
        raise ActionError("Некорректный токен запроса.")
    return token or None


def _same_action_request(action, *, part, location, action_type, quantity, comment, by) -> bool:
    return (
        action.part_type_id == part.pk
        and action.location_id == location.pk
        and action.action_type == action_type
        and action.quantity == quantity
        and action.customer_comment == comment
        and (by is None or action.created_by_id == by.pk)
    )


@transaction.atomic
def _perform_action_atomic(
    *,
    part: PartType,
    location,
    action_type: str,
    quantity,
    customer_comment: str,
    scanned_number: str = "",
    by=None,
    request_token=None,
) -> WarehouseAction:
    """Провести действие со сканера: Продажа / Резерв / Ремонт.

    Остаток меняют ТОЛЬКО существующие сервисы (sales/repairs): они блокируют
    лоты (select_for_update), проверяют доступность с учётом чужих броней и
    пишут движения. Здесь: выбор лотов ячейки (FIFO), сборка документа в один
    шаг и журнальная запись для отчёта. Любая ошибка откатывает всё атомарно —
    отрицательный остаток невозможен.
    """
    if action_type not in WarehouseAction.Type.values:
        raise ActionError("Неизвестный тип действия.")
    customer_comment = (customer_comment or "").strip()
    if not customer_comment:
        raise ActionError("Укажите клиента или комментарий.")
    quantity = parse_quantity(quantity)
    if action_type in {WarehouseAction.Type.SALE, WarehouseAction.Type.REPAIR}:
        require_customs_metadata([part])
    token = _request_token(request_token)
    if token:
        existing = WarehouseAction.objects.filter(request_token=token).first()
        if existing:
            if not _same_action_request(
                existing,
                part=part,
                location=location,
                action_type=action_type,
                quantity=quantity,
                comment=customer_comment,
                by=by,
            ):
                raise ActionError("Токен запроса уже использован для другого действия.")
            return existing

    lots = list(
        StockLot.objects.select_for_update()
        .filter(part_type=part, location=location, status=StockLot.Status.AVAILABLE)
        .order_by("created_at", "pk")
    )
    portions = _split_quantity_over_lots(lots, quantity)

    # Цена нужна и продаже, и записи журнала. Для продажи пустая цена - повод
    # остановиться, а не подставить ноль; резерв и ремонт живут по своим
    # правилам, и их запись в журнале остаётся прежней.
    unit_price = part.recommended_price
    journal_price = unit_price if unit_price is not None else Decimal("0")
    sale = reservation = repair_order = None
    try:
        if action_type == WarehouseAction.Type.SALE:
            check_sale_line_price(part, unit_price)
            sale = create_sale(customer_name=customer_comment, comment="Сканер действий", by=by)
            for lot, portion in portions:
                add_stock_lot_to_sale(sale, lot, portion, unit_price=unit_price, by=by)
            sale = complete_sale(sale, by=by)
        elif action_type == WarehouseAction.Type.RESERVE:
            reservation = create_reservation(
                customer_name=customer_comment, comment="Сканер действий", by=by
            )
            for lot, portion in portions:
                add_stock_lot_to_reservation(reservation, lot, portion, by=by)
            reservation = activate_reservation(reservation, by=by)
        else:  # repair
            repair_order = create_repair_order(
                customer_name=customer_comment, comment="Сканер действий", by=by
            )
            for lot, portion in portions:
                add_stock_lot_to_repair_order(repair_order, lot, portion, by=by)
            repair_order = complete_repair_order(repair_order, by=by)
    except Exception as exc:
        # Понятная ошибка вместо текстов внутренних сервисов, если гонка
        # съела доступность между расчётом порций и проведением.
        if exc.__class__.__name__ in ("SaleError", "ReservationError", "RepairError"):
            raise ActionError(str(exc)) from exc
        raise

    return WarehouseAction.objects.create(
        action_type=action_type,
        request_token=token,
        part_type=part,
        # Снимок личности: точный номер, что сканировали/продали.
        part_number=identity_number(part, scanned_number),
        part_name=part.name,
        manufacturer_name=_manufacturer_snapshot(part),
        location=location,
        location_code=location.code,
        quantity=quantity,
        unit_price_rub=journal_price,
        total_price_rub=money(journal_price * quantity),
        price_source_number=_price_source_number(part),
        customer_comment=customer_comment,
        sale=sale,
        reservation=reservation,
        repair_order=repair_order,
        created_by=by,
    )


def perform_action(
    *,
    part: PartType,
    location,
    action_type: str,
    quantity,
    customer_comment: str,
    scanned_number: str = "",
    by=None,
    request_token=None,
) -> WarehouseAction:
    """Run one scanner mutation and safely reuse a repeated request token."""
    scanned_value = clean_lookup_value(scanned_number)
    if scanned_value:
        lookup = resolve_part_lookup(scanned_value)
        selected_is_exact = lookup.found and lookup.candidate.part.pk == part.pk
        if lookup.ambiguous:
            selected_is_exact = any(
                candidate.part.pk == part.pk
                and candidate.match_source in {MatchSource.EXACT, MatchSource.BARCODE}
                for candidate in lookup.candidates
            )
        if not selected_is_exact:
            raise ActionError(IDENTITY_MISMATCH_MESSAGE)
    token = _request_token(request_token)
    try:
        return _perform_action_atomic(
            part=part,
            location=location,
            action_type=action_type,
            quantity=quantity,
            customer_comment=customer_comment,
            scanned_number=scanned_number,
            by=by,
            request_token=token,
        )
    except IntegrityError:
        # A concurrent request can win the unique-token race. Its transaction
        # is now visible and this request has been rolled back in full.
        existing = WarehouseAction.objects.filter(request_token=token).first() if token else None
        if existing:
            try:
                parsed_quantity = Decimal(str(quantity).replace(",", "."))
            except (InvalidOperation, TypeError):
                raise ActionError("Некорректное количество.") from None
            if _same_action_request(
                existing,
                part=part,
                location=location,
                action_type=action_type,
                quantity=parsed_quantity,
                comment=(customer_comment or "").strip(),
                by=by,
            ):
                return existing
        raise


# --- Отмена ошибочной продажи --------------------------------------------------------


@transaction.atomic
def cancel_warehouse_action(action: WarehouseAction, *, by=None, reason="") -> WarehouseAction:
    """Отменить ошибочную ПРОДАЖУ: вернуть остаток в ту же ячейку и сторнировать.

    Остаток возвращается существующим inventory.return_stock_lot_quantity
    (движение RETURN_LOT, компенсирующее продажу — аудит сохраняется); Sale
    помечается VOIDED (уходит из отчётов/статистики, они считают только
    completed); действие — CANCELLED с автором/временем/причиной. Всё
    атомарно, лоты блокируются внутри return_*; отрицательный остаток
    невозможен. Резерв/ремонт этой командой не отменяются.
    """
    action = WarehouseAction.objects.select_for_update().get(pk=action.pk)
    if action.status == WarehouseAction.Status.CANCELLED:
        raise ActionError("Действие уже отменено.")
    if action.action_type != WarehouseAction.Type.SALE:
        raise ActionError("Отмена поддержана только для продаж.")
    reason = (reason or "").strip()
    if not reason:
        raise ActionError("Укажите причину отмены.")
    if action.sale_id is None:
        raise ActionError("У продажи нет связанного документа: отмена невозможна.")
    sale = Sale.objects.select_for_update().get(pk=action.sale_id)
    if sale.status == Sale.Status.VOIDED:
        raise ActionError("Связанная продажа уже сторнирована.")

    # Report and action entry points must use the same service: it restores
    # serial items too and subtracts already completed customer returns.
    from apps.sales.services import cancel_sale

    cancel_sale(sale, by=by, reason=reason, author=str(by or "Система"))
    # Keep the historical action endpoint's public document status compatible.
    # Both CANCELED and VOIDED are excluded from commercial reports.
    sale.status = Sale.Status.VOIDED
    sale.save(update_fields=["status", "updated_at"])
    now = timezone.now()

    # Мультипозиционная продажа — это один документ и несколько записей
    # журнала. Сторно документа отменяет их все: иначе в отчёте остались бы
    # «активные» строки уже возвращённого товара.
    WarehouseAction.objects.filter(
        sale_id=sale.pk, status=WarehouseAction.Status.ACTIVE
    ).update(
        status=WarehouseAction.Status.CANCELLED,
        cancelled_at=now,
        cancelled_by=by,
        cancel_reason=reason,
    )
    action.refresh_from_db()
    return action


@transaction.atomic
def repair_action_identity_snapshot(
    action: WarehouseAction, *, part_number: str
) -> WarehouseAction:
    """Исправить ошибочный snapshot номера без изменения складской физики.

    Используется для исторических действий, созданных до сохранения
    `scanned_number`: автоматический backfill мог взять primary/OEM карточки,
    хотя фактически продавали номер-замену с той же карточки. Проверяем, что
    новый номер уже принадлежит той же `PartType`; остатки, продажи и движения
    не трогаем.
    """
    action = (
        WarehouseAction.objects.select_for_update()
        .select_related("part_type", "location")
        .get(pk=action.pk)
    )
    norm = normalize_number(part_number or "")
    if not norm:
        raise ActionError("Укажите корректный номер детали.")
    matched = (
        PartNumber.objects.filter(part=action.part_type, normalized_value=norm)
        .order_by("-is_primary", "pk")
        .first()
    )
    if matched is None:
        raise ActionError("Этот номер не принадлежит карточке детали действия.")

    update_fields = []
    if action.part_number != matched.value:
        action.part_number = matched.value
        update_fields.append("part_number")
    if not action.part_name:
        action.part_name = action.part_type.name
        update_fields.append("part_name")
    if not action.location_code:
        action.location_code = action.location.code
        update_fields.append("location_code")
    if update_fields:
        action.save(update_fields=update_fields)
    return action


# --- Единый отчёт действий -----------------------------------------------------------


def actions_report(
    *, date_from=None, date_to=None, action_type="", q="", part_number="",
    location_code="", include_cancelled=False,
):
    """Отфильтрованный журнал действий + итоги (количество и сумма).

    Отменённые действия по умолчанию исключены (не входят в итоги, таможню и
    Excel); include_cancelled=True показывает их отдельно для аудита.
    """
    qs = with_part_identity(
        WarehouseAction.objects.select_related(
            "part_type",
            "location",
            "created_by",
            "cancelled_by",
            "sale",
            "reservation",
            "repair_order",
            "stock_return",
        )
    )
    if not include_cancelled:
        qs = qs.filter(status=WarehouseAction.Status.ACTIVE)
    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if action_type:
        qs = qs.filter(action_type=action_type)
    if q:
        qs = qs.filter(customer_comment__icontains=q)
    if part_number:
        norm = normalize_number(part_number)
        part_ids = PartNumber.objects.filter(normalized_value=norm).values_list(
            "part_id", flat=True
        )
        qs = qs.filter(
            Q(part_number__icontains=part_number)
            | Q(part_type_id__in=list(part_ids))
            | Q(part_type__name__icontains=part_number)
        )
    if location_code:
        # Фильтр принимает операторский 1-1-1 и хранимый S01-D01-C01: снимок
        # адреса в журнале записан длинной формой, а сотрудник вводит короткую.
        from apps.warehouse.addresses import normalize_address_input

        location_match = Q()
        for term in {location_code, normalize_address_input(location_code)}:
            location_match |= (
                Q(location_code__icontains=term) | Q(location__code__icontains=term)
            )
        qs = qs.filter(location_match)
    totals_qs = qs.exclude(status=WarehouseAction.Status.CANCELLED)
    totals = totals_qs.aggregate(quantity=Sum("quantity"), value=Sum("total_price_rub"))
    return qs, {
        "quantity": totals["quantity"] or Decimal("0"),
        "value": totals["value"] or Decimal("0"),
    }


# --- Таможенные данные ----------------------------------------------------------------

# Простой пословный перевод английских названий деталей для графы «НАЗВАНИЕ
# ТОВАРА НА РУССКОМ ЯЗЫКЕ». Это таможенное, а не техническое название: слова
# вне словаря остаются как есть (в верхнем регистре), пользователь может
# поправить название вручную (источник станет manual).
RU_WORDS = {
    "SCREW": "ВИНТ", "BOLT": "БОЛТ", "NUT": "ГАЙКА", "WASHER": "ШАЙБА",
    "GASKET": "ПРОКЛАДКА", "SEAL": "САЛЬНИК", "O-RING": "КОЛЬЦО", "RING": "КОЛЬЦО",
    "BEARING": "ПОДШИПНИК", "BELT": "РЕМЕНЬ", "ROLLER": "РОЛИК", "PULLEY": "ШКИВ",
    "SPRING": "ПРУЖИНА", "FILTER": "ФИЛЬТР", "PUMP": "НАСОС", "HOSE": "ШЛАНГ",
    "CLAMP": "ХОМУТ", "BRACKET": "КРОНШТЕЙН", "COVER": "КРЫШКА", "CAP": "КОЛПАЧОК",
    "PLUG": "ЗАГЛУШКА", "SENSOR": "ДАТЧИК", "SWITCH": "ВЫКЛЮЧАТЕЛЬ", "CABLE": "ТРОС",
    "WIRE": "ПРОВОД", "GUARD": "ЗАЩИТА", "SHAFT": "ВАЛ", "GEAR": "ШЕСТЕРНЯ",
    "SPROCKET": "ЗВЕЗДА", "CHAIN": "ЦЕПЬ", "PISTON": "ПОРШЕНЬ", "VALVE": "КЛАПАН",
    "KIT": "КОМПЛЕКТ", "LEVER": "РЫЧАГ", "HANDLE": "РУКОЯТКА", "PIN": "ШТИФТ",
    "DECAL": "НАКЛЕЙКА", "LABEL": "НАКЛЕЙКА", "BUMPER": "БАМПЕР", "PANEL": "ПАНЕЛЬ",
    "WINDSHIELD": "СТЕКЛО", "MIRROR": "ЗЕРКАЛО", "LAMP": "ФОНАРЬ", "LIGHT": "ФОНАРЬ",
    "BATTERY": "АККУМУЛЯТОР", "STARTER": "СТАРТЕР", "GENERATOR": "ГЕНЕРАТОР",
    "CARBURETOR": "КАРБЮРАТОР", "INJECTOR": "ФОРСУНКА", "RADIATOR": "РАДИАТОР",
    "THERMOSTAT": "ТЕРМОСТАТ", "IMPELLER": "КРЫЛЬЧАТКА", "TRACK": "ГУСЕНИЦА",
    "SKI": "ЛЫЖА", "BUSHING": "ВТУЛКА", "SPACER": "ПРОСТАВКА", "SHIM": "ШАЙБА",
    "RETAINER": "ФИКСАТОР", "ADAPTER": "ПЕРЕХОДНИК", "CONNECTOR": "РАЗЪЁМ",
    "FUSE": "ПРЕДОХРАНИТЕЛЬ", "RELAY": "РЕЛЕ", "HOOD": "КАПОТ", "SEAT": "СИДЕНЬЕ",
    "FENDER": "КРЫЛО", "AXLE": "ОСЬ", "HUB": "СТУПИЦА", "DISC": "ДИСК", "DISK": "ДИСК",
    "PAD": "КОЛОДКА", "BRAKE": "ТОРМОЗ", "CALIPER": "СУППОРТ", "WHEEL": "КОЛЕСО",
    "TIRE": "ШИНА", "TUBE": "ТРУБКА", "PIPE": "ТРУБА", "EXHAUST": "ГЛУШИТЕЛЬ",
    "MUFFLER": "ГЛУШИТЕЛЬ", "DRIVE": "ПРИВОД", "CLUTCH": "СЦЕПЛЕНИЕ",
    "DAMPER": "ДЕМПФЕР", "ABSORBER": "АМОРТИЗАТОР", "SHOCK": "АМОРТИЗАТОР",
    "STRAP": "РЕМЕШОК", "LATCH": "ЗАЩЁЛКА", "HINGE": "ПЕТЛЯ", "KNOB": "РУЧКА",
    "GROMMET": "ВТУЛКА", "BOOT": "ПЫЛЬНИК", "JOINT": "ШАРНИР", "ARM": "РЫЧАГ",
    "ROD": "ТЯГА", "LINK": "ТЯГА", "STUD": "ШПИЛЬКА", "RIVET": "ЗАКЛЁПКА",
    "HEX": "ШЕСТИГРАННЫЙ", "HEX.": "ШЕСТИГРАННЫЙ", "FLANGED": "ФЛАНЦЕВЫЙ",
    "OIL": "МАСЛО", "GRIP": "РУЧКА", "MOUNT": "ОПОРА", "SUPPORT": "ОПОРА",
    "LENS": "ЛИНЗА", "ROTOR": "РОТОР", "PRESSURE": "ДАВЛЕНИЕ", "DISTANCE": "ДИСТАНЦИОННЫЙ",
    "HOUSING": "КОРПУС", "NEEDLE": "ИГОЛЬЧАТЫЙ", "CIRCLIP": "СТОПОРНОЕ КОЛЬЦО",
    "SOCKET": "ПАТРУБОК", "CAMSHAFT": "РАСПРЕДЕЛИТЕЛЬНЫЙ ВАЛ", "CUSHION": "ПОДУШКА",
    "CYLINDER": "ЦИЛИНДР", "HEAD": "ГОЛОВКА", "AIR": "ВОЗДУШНЫЙ", "VIBRATION": "ВИБРАЦИЯ",
    "FUEL": "ТОПЛИВНЫЙ", "LOWER": "НИЖНИЙ", "STOPPER": "ОГРАНИЧИТЕЛЬ", "SUSPENSION": "ПОДВЕСКА",
    "WEAR": "ИЗНОСОСТОЙКИЙ", "STATOR": "СТАТОР", "CRANK": "КРИВОШИП", "WEB": "ЩЕКА",
}


RU_PHRASES = {
    "PISTON ASS'Y WITH RINGS": "ПОРШЕНЬ В СБОРЕ С КОЛЬЦАМИ",
    "PISTON ASS'Y": "ПОРШЕНЬ В СБОРЕ",
    "BUSHING SUSPENSION ARM KIT": "КОМПЛЕКТ ВТУЛОК РЫЧАГА ПОДВЕСКИ",
    "CYLINDER HEAD GASKET": "ПРОКЛАДКА ГОЛОВКИ ЦИЛИНДРА",
    "OIL PRESSURE SWITCH": "ДАТЧИК ДАВЛЕНИЯ МАСЛА",
    "OIL PUMP ROTOR": "РОТОР МАСЛЯНОГО НАСОСА",
    "GASKET VALVE ROD HOUSING": "ПРОКЛАДКА КОРПУСА ШТОКА КЛАПАНА",
    "HEX. DISTANCE SCREW": "ДИСТАНЦИОННЫЙ ШЕСТИГРАННЫЙ ВИНТ",
    "NEEDLE BEARING": "ИГОЛЬЧАТЫЙ ПОДШИПНИК",
    "BEARING NEEDLE": "ИГОЛЬЧАТЫЙ ПОДШИПНИК",
    "PISTON CIRCLIP": "СТОПОРНОЕ КОЛЬЦО ПОРШНЯ",
    "CARBURETOR SOCKET": "ПАТРУБОК КАРБЮРАТОРА",
    "CAMSHAFT CHAIN": "ЦЕПЬ РАСПРЕДЕЛИТЕЛЬНОГО ВАЛА",
    "PISTON PIN": "ПАЛЕЦ ПОРШНЯ",
    "RUBBER RING": "РЕЗИНОВОЕ КОЛЬЦО",
    "AIR FILTER": "ВОЗДУШНЫЙ ФИЛЬТР",
    "FILTER FUEL": "ТОПЛИВНЫЙ ФИЛЬТР",
    "DAMPER VIBRATION": "ВИБРОДЕМПФЕР",
    "LOWER STOPPER": "НИЖНИЙ ОГРАНИЧИТЕЛЬ",
    "WHEEL CAP": "КОЛПАК КОЛЕСА",
    "RUBBER BOOT": "РЕЗИНОВЫЙ ПЫЛЬНИК",
    "BALL JOINT": "ШАРОВАЯ ОПОРА",
    "KIT SPRING SUPPORT": "КОМПЛЕКТ ОПОРЫ ПРУЖИНЫ",
    "SPI STATOR SKI DOO": "SPI СТАТОР SKI-DOO",
    "SPI PTO CRANK WEB": "SPI ЩЕКА КРИВОШИПА PTO",
    "ROLLER PULLEY": "РОЛИК ШКИВА",
    "OIL SEAL": "САЛЬНИК",
    "HOUSING GASKET": "ПРОКЛАДКА КОРПУСА",
    "WEAR RING": "ИЗНОСОСТОЙКОЕ КОЛЬЦО",
    "MAINTENANCE CLUTCH KIT": "КОМПЛЕКТ ОБСЛУЖИВАНИЯ СЦЕПЛЕНИЯ",
    "VALVE STEM SEAL": "МАСЛОСЪЁМНЫЙ КОЛПАЧОК КЛАПАНА",
    "OIL PUMP COVER": "КРЫШКА МАСЛЯНОГО НАСОСА",
    "OIL HOSE": "МАСЛЯНЫЙ ШЛАНГ",
    "SPARK PLUG": "СВЕЧА ЗАЖИГАНИЯ",
    "BALL BEARING": "ШАРИКОВЫЙ ПОДШИПНИК",
    "O-RING": "УПЛОТНИТЕЛЬНОЕ КОЛЬЦО",
    "ROLLER PULLER": "СЪЁМНИК РОЛИКА",
    "PIN ROLLER": "ОСЬ РОЛИКА",
    "SLIDER SHOE": "БАШМАК СКОЛЬЖЕНИЯ",
    "BELT DRIVE": "ПРИВОДНОЙ РЕМЕНЬ",
    "DRIVE BELT": "ПРИВОДНОЙ РЕМЕНЬ",
    "BUSHING HALF": "ПОЛОВИНА ВТУЛКИ",
    "HALF BUSHING": "ПОЛОВИНА ВТУЛКИ",
    "PIN SPRING": "ПРУЖИННЫЙ ШТИФТ",
    "OETIKER CLAMP": "ХОМУТ OETIKER",
}


def _normalize_customs_dimensions(text: str) -> str:
    import re

    text = re.sub(
        r"(?i)(\d+(?:\.\d+)?)\s*MM\s*X\s*(\d+(?:\.\d+)?)\s*MM",
        lambda m: f"{m.group(1).replace('.', ',')} × "
        f"{m.group(2).replace('.', ',')} ММ",
        text,
    )
    text = re.sub(r"(?i)(\d+(?:\.\d+)?)\s*MM\s*LONG",
                  lambda m: f"ДЛИНОЙ {m.group(1).replace('.', ',')} ММ", text)
    return text


def auto_customs_name_ru(english_name: str) -> str:
    """Детерминированный phrase-first перевод каталожного названия.

    Целые технические выражения заменяются до отдельных слов. Неизвестные
    латинские токены сохраняются только как коды, бренды и размеры, поэтому
    продуктовая сущность не остаётся англо-русской смесью.
    """
    import re

    text = re.sub(r"[_]+", " ", (english_name or "").upper())
    text = _normalize_customs_dimensions(text)
    for phrase in sorted(RU_PHRASES, key=len, reverse=True):
        text = re.sub(rf"(?<![A-ZА-Я]){re.escape(phrase)}(?![A-ZА-Я])", RU_PHRASES[phrase], text)
    words = text.split()
    translated = []
    for word in words:
        translated.append(RU_WORDS.get(word) or RU_WORDS.get(word.strip(".,")) or word)
    result = " ".join(translated).strip()
    result = re.sub(r"\s+", " ", result)
    # Technical brand/model/code tokens are allowed; remaining ordinary
    # English words are surfaced by the audit instead of hidden in an allowlist.
    return result


def _customs_defaults(part: PartType) -> dict:
    if _polaris_part_for(part) is not None:
        return {"manufacturer": "POLARIS", "country_of_origin": ""}
    return {}


def get_or_create_customs(part: PartType) -> PartCustomsInfo:
    """Таможенная карточка детали (создаёт строку). Только для страницы правки."""
    info, _created = PartCustomsInfo.objects.get_or_create(
        part_type=part, defaults=_customs_defaults(part)
    )
    return info


def read_customs(part: PartType) -> PartCustomsInfo:
    """Таможенная карточка ТОЛЬКО для чтения: отсутствующую не сохраняет.

    Отчёт и экспорт (GET) не должны писать в базу; строка появляется, когда
    пользователь реально правит таможенные данные.
    """
    info = PartCustomsInfo.objects.filter(part_type=part).first()
    if info is not None:
        return info
    return PartCustomsInfo(part_type=part, **_customs_defaults(part))  # не сохраняем


def record_customs_data_version(
    customs: PartCustomsInfo, *, by=None
) -> PartCustomsDataVersion | None:
    """Дописать неизменяемую версию таможенных данных, введённых оператором.

    Значений каталога здесь нет намеренно. Сохранение без изменений
    идемпотентно, а настоящая правка становится новой исторической версией.
    """
    fields = (
        "customs_name_ru", "customs_name_ru_confirmed", "customs_name_en",
        "manufacturer", "country_of_origin",
        "gross_weight_kg", "net_weight_kg", "customs_unit_price_usd",
        "application_area", "source_reference",
    )
    values = {field: getattr(customs, field) for field in fields}
    # Открытие формы правки заводит карточку со значениями по умолчанию - это
    # ещё не заявление пользователя. Записать её версией нельзя: она станет
    # самой ранней и перехватит всю историю списаний, оставив декларацию
    # пустой. Значением по умолчанию считается и «BRP» у производителя.
    untouched = PartCustomsInfo(
        part_type=customs.part_type, **_customs_defaults(customs.part_type)
    )
    if all(getattr(untouched, field) == value for field, value in values.items()):
        return None
    previous = customs.part_type.customs_data_versions.order_by("-version").first()
    unchanged = previous is not None and all(
        getattr(previous, field) == value for field, value in values.items()
    )
    if unchanged:
        return previous
    return PartCustomsDataVersion.objects.create(
        part_type=customs.part_type,
        version=(previous.version + 1) if previous is not None else 1,
        effective_from=timezone.now(),
        created_by=by or customs.updated_by,
        **values,
    )


# Утверждённый business fallback только для BRP без явно сохранённой страны.
# Это правило компании, а не вывод о стране из каталога или названия бренда.
CUSTOMS_COUNTRY = "CANADA"


def resolve_customs_country(part: PartType, explicit_country: str = "", number: str = "") -> str:
    """Сохранённая страна имеет приоритет; только BRP получает fallback.

    Не используем manufacturer_display: его значение по умолчанию не
    доказывает принадлежность детали к BRP. Принадлежность доказывают связь
    BrpPartLink, подпись производителя BRP или exact-артикул в актуальном
    прайсе BRP. Конкурирующая связь с другим каталогом исключает fallback
    даже при устаревшей подписи BRP.
    """
    if country := (explicit_country or "").strip():
        return country.upper()
    if (
        PolarisPartLink.objects.filter(part=part).exists()
        or AftermarketCatalogPart.objects.filter(part=part).exists()
    ):
        return ""
    manufacturer = part.manufacturer.name.strip().upper() if part.manufacturer_id else ""
    if manufacturer and manufacturer != "BRP":
        return ""
    if (
        manufacturer == "BRP"
        or BrpPartLink.objects.filter(part=part).exists()
        or _brp_catalog_for_number(number) is not None
    ):
        return CUSTOMS_COUNTRY
    return ""


def catalog_english_name(part: PartType, number: str = "") -> str:
    """Английское название детали из каталога поставщика.

    Источник ровно один и тот же, что уже показывает карточку: описание
    позиции BRP (``BrpCatalogPart.part_desc``), название позиции Polaris
    (``PolarisCatalogPart.part_name``) или описание aftermarket-позиции
    (``AftermarketCatalogPart.source_description``). Связь карточки сильнее,
    чем exact-совпадение артикула; без каталожного описания название
    остаётся пустым - придумывать его нельзя.
    """
    brp = _brp_part_for(part) or _brp_catalog_for_number(number)
    if brp is not None and brp.part_desc.strip():
        return brp.part_desc.strip().upper()
    polaris = _polaris_part_for(part) or _polaris_catalog_for_number(number)
    if polaris is not None and polaris.part_name.strip():
        return polaris.part_name.strip().upper()
    aftermarket = _aftermarket_part_for(part) or _aftermarket_catalog_for_number(number)
    if aftermarket is not None and aftermarket.source_description.strip():
        return aftermarket.source_description.strip().upper()
    return ""


def catalog_manufacturer_name(part: PartType, number: str = "") -> str:
    """Производитель по доказанной связи с каталогом поставщика.

    Не путать с ``manufacturer_display``: подпись справочника карточки сама
    по себе ничего не доказывает, поэтому используется только как часть
    aftermarket-записи. Без связи с каталогом производитель остаётся пустым.
    """
    if _brp_part_for(part) is not None or _brp_catalog_for_number(number) is not None:
        return "BRP"
    if _polaris_part_for(part) is not None or _polaris_catalog_for_number(number) is not None:
        return "POLARIS"
    aftermarket = _aftermarket_part_for(part) or _aftermarket_catalog_for_number(number)
    if aftermarket is not None and aftermarket.manufacturer_id:
        return aftermarket.manufacturer.name.strip().upper()
    return ""


def catalog_customs_usd(part: PartType, number: str = "") -> Decimal | None:
    """Таможенная стоимость единицы в USD из каталога поставщика.

    Это оптовая (дилерская) колонка прайса: у BRP - ``wholesale_price_usd``
    самой позиции либо связанной замены, у Polaris - та же колонка позиции
    либо её superseded-связи, у aftermarket - ``dealer_cost_usd`` (та же
    оптовая USD, см. aftermarket_catalog._customer_price_rub). Розница,
    клиентская цена и складская себестоимость сюда не подмешиваются: они
    отвечают на другие вопросы. Нет оптовой цены - остаётся ``None``.
    """
    brp = _brp_part_for(part) or _brp_catalog_for_number(number)
    if brp is not None:
        return _brp_wholesale_usd(brp)
    polaris = _polaris_part_for(part) or _polaris_catalog_for_number(number)
    if polaris is not None:
        return _polaris_wholesale_usd(polaris)
    aftermarket = _aftermarket_part_for(part) or _aftermarket_catalog_for_number(number)
    if aftermarket is not None and aftermarket.dealer_cost_usd and aftermarket.dealer_cost_usd > 0:
        return aftermarket.dealer_cost_usd
    return None


def system_customs_facts(part: PartType) -> dict:
    """То, что DenisStock знает о детали сам. Оператор это не вводит.

    Возвращаются ровно те значения, которые уйдут в карточку при сохранении:
    английское название и цена из каталога поставщика (по связи карточки или
    exact-артикулу), производитель по канонической связи карточки, страна по
    новому правилу. Отсутствующее остаётся пустым - выдумывать таможенные
    факты нельзя.
    """
    number = part_exact_number(part, default="")
    return {
        "customs_name_en": catalog_english_name(part, number),
        "manufacturer": manufacturer_display(part).strip().upper(),
        "country_of_origin": resolve_customs_country(
            part, read_customs(part).country_of_origin, number
        ),
        "customs_unit_price_usd": catalog_customs_usd(part, number),
    }


def apply_system_customs_facts(customs: PartCustomsInfo) -> dict:
    """Проставить карточке автоматические значения перед сохранением.

    Записываются они именно в карточку, а не только в экспорт: историческая
    версия снимает состояние карточки, и без записи снимок остался бы пустым.
    """
    facts = system_customs_facts(customs.part_type)
    for field, value in facts.items():
        setattr(customs, field, value)
    return facts


def _brp_part_for(part: PartType):
    link = BrpPartLink.objects.filter(part=part).select_related("brp_part").first()
    return link.brp_part if link else None


def _polaris_part_for(part: PartType):
    link = (
        PolarisPartLink.objects.filter(part=part)
        .select_related("polaris_part")
        .first()
    )
    return link.polaris_part if link else None


def _aftermarket_part_for(part: PartType):
    return AftermarketCatalogPart.objects.filter(part=part).order_by("pk").first()


def _brp_catalog_for_number(number: str):
    """Exact-позиция актуального BRP прайса по каноническому артикулу строки."""
    norm = normalize_number(number)
    if not norm:
        return None
    return BrpCatalogPart.objects.filter(material_no_norm=norm, is_current=True).first()


def _polaris_catalog_for_number(number: str):
    norm = normalize_number(number)
    if not norm:
        return None
    return PolarisCatalogPart.objects.filter(part_number_norm=norm).first()


def _aftermarket_catalog_for_number(number: str):
    norm = normalize_number(number)
    if not norm:
        return None
    return AftermarketCatalogPart.objects.filter(
        normalized_manufacturer_number=norm
    ).order_by("pk").first()


def _brp_wholesale_usd(brp) -> Decimal | None:
    """Оптовая (dealer) цена BRP в USD: сама позиция, иначе связанная замена.

    Колонка «ОПТОВАЯ» прайса BRP. Replacement — ТОЛЬКО источник цены: номер
    детали (material_no) от этого не меняется. Розница и клиентская цена в
    таможенную форму не подмешиваются.
    """
    if brp.wholesale_price_usd and brp.wholesale_price_usd > 0:
        return brp.wholesale_price_usd
    related = Q()
    if brp.material_no_norm:
        related |= Q(replacement_no_1_norm=brp.material_no_norm)
        related |= Q(replacement_no_2_norm=brp.material_no_norm)
    for repl in (brp.replacement_no_1_norm, brp.replacement_no_2_norm):
        if repl:
            related |= Q(material_no_norm=repl)
    if not related:
        return None
    source = (
        BrpCatalogPart.objects.filter(is_current=True, wholesale_price_usd__gt=0)
        .filter(related)
        .order_by("pk")
        .first()
    )
    return source.wholesale_price_usd if source else None


def _polaris_wholesale_usd(polaris) -> Decimal | None:
    """Оптовая цена Polaris в USD: сама позиция, иначе superseded-связь.

    Superseded — ТОЛЬКО источник цены: part_number не подменяется.
    """
    if polaris.wholesale_price_usd and polaris.wholesale_price_usd > 0:
        return polaris.wholesale_price_usd
    related = Q()
    if polaris.part_number_norm:
        related |= Q(superseded_number_norm=polaris.part_number_norm)
    if polaris.superseded_number_norm:
        related |= Q(part_number_norm=polaris.superseded_number_norm)
    if not related:
        return None
    source = (
        PolarisCatalogPart.objects.filter(related, wholesale_price_usd__gt=0)
        .order_by("pk")
        .first()
    )
    return source.wholesale_price_usd if source else None


# Вид техники (справочник) -> таможенная область применения. Мотоциклы компания
# не обслуживает: их применимость НЕ превращается в «МОТО ЗАПЧАСТИ», строка
# просто остаётся пустой. Значения - из единого списка PartCustomsInfo.
# ApplicationArea: та же таблица категорий, что предлагает ручной select.
_ApplicationArea = PartCustomsInfo.ApplicationArea
_APPLICATION_BY_VEHICLE_TYPE = {
    "снегоход": _ApplicationArea.SNOWMOBILE,
    "квадроцикл": _ApplicationArea.ATV,
    "гидроцикл": _ApplicationArea.WATERCRAFT,
    "катер": _ApplicationArea.BOAT,
    "лодка": _ApplicationArea.BOAT,
    "яхта": _ApplicationArea.BOAT,
    "автомобиль": _ApplicationArea.CAR,
}
MULTI_APPLICATION = _ApplicationArea.UNIVERSAL
# Старый хардкод модели (прежний default application_area). Не входит в
# ApplicationArea.choices намеренно: в таможенную форму не выгружается
# никогда, а явное ручное значение с ним никогда не совпадёт.
LEGACY_APPLICATION = "МОТО ЗАПЧАСТИ"


def resolve_customs_application(part: PartType) -> str:
    """Область применения по ФАКТИЧЕСКОЙ применимости детали.

    Источник — только данные каталога: PartCompatibility -> VehicleModel ->
    VehicleMake -> VehicleType. Ни названия детали, ни производителя каталога
    (BRP/Polaris) для догадок не используются.

    Одна обслуживаемая категория -> она; несколько -> «УНИВЕРСАЛЬНЫЕ ЗАПЧАСТИ»;
    нет данных или только необслуживаемая техника (мотоциклы) -> пустая строка.
    """
    names = (
        VehicleType.objects.filter(makes__models__compatibilities__part=part)
        .values_list("name", flat=True)
        .distinct()
    )
    categories = {
        _APPLICATION_BY_VEHICLE_TYPE[name.strip().lower()]
        for name in names
        if name and name.strip().lower() in _APPLICATION_BY_VEHICLE_TYPE
    }
    if not categories:
        return ""  # надёжно определить нельзя — не выдумываем
    if len(categories) > 1:
        return str(MULTI_APPLICATION)
    return str(next(iter(categories)))


# Вес одной штуки: max_digits=8, decimal_places=3 у PartCustomsInfo -> целая
# часть максимум 5 цифр (99999.999 кг). Значение вне этого диапазона Postgres
# бы тихо округлил/обрезал при записи — валидируем в Python заранее.
_MAX_WEIGHT_KG = Decimal("100000")

# Заметка-маркер, которую быстрый редактор пишет в weight_source_note при
# ручном вводе обоих весов (URL не выдумывается). Для классификации источника
# она НЕ считается внешним источником: «Указано вручную», а не «Получено из
# источника».
MANUAL_WEIGHT_NOTE = "Указано вручную сотрудником"


def parse_weight_kg(raw) -> Decimal | None:
    """Вес одной штуки в кг: Decimal с точностью до 3 знаков, строго > 0.

    Пустая строка/None -> None («не заполнено» - разрешённое состояние, вес
    не выдумывается). Ноль как «подтверждённый» вес запрещён явно - это не
    то же самое, что «не заполнено». Отрицательные значения, NaN, Infinity,
    больше 3 знаков после запятой и значения вне диапазона поля - ValueError
    с понятным текстом для пользователя.
    """
    raw = (str(raw) if raw is not None else "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Вес должен быть числом в кг.") from exc
    if not value.is_finite():  # ловит и NaN, и Infinity
        raise ValueError("Вес должен быть числом в кг.")
    if value <= 0:
        raise ValueError("Вес должен быть больше нуля.")
    exponent = value.as_tuple().exponent
    if isinstance(exponent, int) and exponent < -3:
        raise ValueError("Вес: не более 3 знаков после запятой.")
    if value >= _MAX_WEIGHT_KG:
        raise ValueError("Вес слишком большой.")
    return value


def parse_weight_g(raw) -> Decimal | None:
    """Actual per-unit weight entered by the operator in grams.

    Storage remains the established Decimal kg representation: three decimal
    places are exact whole-gram precision and avoid a destructive schema fork.
    """
    raw = (str(raw) if raw is not None else "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        grams = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Вес должен быть целым числом в граммах.") from exc
    if not grams.is_finite() or grams <= 0 or grams != grams.to_integral_value():
        raise ValueError("Вес должен быть целым числом в граммах.")
    kg = grams / Decimal("1000")
    if kg >= _MAX_WEIGHT_KG:
        raise ValueError("Вес слишком большой.")
    return kg


def weight_kg_as_grams(weight_kg: Decimal | None) -> int | None:
    """Обратное преобразование для подстановки в операторскую форму (граммы)."""
    if weight_kg is None:
        return None
    return int((Decimal(weight_kg) * 1000).to_integral_value())


# Таможенный минимум строки выгрузки. Он НЕ является весом детали: запомненный
# фактический вес остаётся тем, что ввёл сотрудник, и минимум применяется
# только в момент формирования таможенной формы.
CUSTOMS_MIN_EXPORT_WEIGHT_KG = Decimal("0.03")


def customs_export_weight_kg(actual_weight_kg: Decimal | None) -> Decimal | None:
    """Customs minimum applies at export only; remembered actual weight is unchanged."""
    if actual_weight_kg is None:
        return None
    return max(Decimal(actual_weight_kg), CUSTOMS_MIN_EXPORT_WEIGHT_KG)


# Список областей применения, который выбирает сотрудник в быстрых действиях.
# Это операторский словарь: автоопределение по совместимости каталога
# (resolve_customs_application) остаётся со своим собственным набором значений
# и здесь не участвует.
QUICK_ACTION_APPLICATION_AREAS = (
    _ApplicationArea.WATERCRAFT,
    _ApplicationArea.ATV,
    _ApplicationArea.SNOWMOBILE,
    _ApplicationArea.OUTBOARD_MOTOR,
    _ApplicationArea.BOAT_CRAFT,
)
APPLICATION_UNSET_LABEL = "Не выбрано"


def parse_application_area(raw) -> str:
    """Область применения из операторской формы: только значение из списка."""
    value = (str(raw) if raw is not None else "").strip().upper()
    if not value:
        return ""
    if value not in {str(area) for area in QUICK_ACTION_APPLICATION_AREAS}:
        raise ValueError("Выберите область применения из списка.")
    return value


def customs_metadata_gaps(
    *, gross_weight_kg: Decimal | None, net_weight_kg: Decimal | None, application_area: str
) -> list[str]:
    """Чего не хватает строке, чтобы операция стала таможенным источником."""
    gaps = []
    if gross_weight_kg is None:
        gaps.append("не заполнен вес брутто")
    if net_weight_kg is None:
        gaps.append("не заполнен вес нетто")
    if not (application_area or "").strip():
        gaps.append("не выбрана область применения")
    elif application_area not in {str(area) for area in QUICK_ACTION_APPLICATION_AREAS}:
        gaps.append("выбрана недопустимая область применения")
    return gaps


def require_customs_metadata(parts) -> None:
    """Fail closed before a new Sale/Repair can become a customs source."""
    missing = []
    for part in {part.pk: part for part in parts}.values():
        customs = read_customs(part)
        try:
            validate_weight_pair(customs.gross_weight_kg, customs.net_weight_kg)
        except ValueError as exc:
            missing.append(f"{part.name}: {exc}")
            continue
        gaps = customs_metadata_gaps(
            gross_weight_kg=customs.gross_weight_kg,
            net_weight_kg=customs.net_weight_kg,
            application_area=customs.application_area,
        )
        if gaps:
            missing.append(f"{part.name}: {', '.join(gaps)}")
    if missing:
        raise ActionError("Для таможенной формы не хватает данных. " + ". ".join(missing))


def parse_customs_usd(raw) -> Decimal | None:
    """Явная таможенная стоимость единицы: подстановки цены каталога здесь нет."""
    raw = (str(raw) if raw is not None else "").strip().replace(",", ".")
    if not raw:
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError("Таможенная цена должна быть числом в USD.") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError("Таможенная цена должна быть больше нуля.")
    if value.as_tuple().exponent < -2:
        raise ValueError("Таможенная цена: не более 2 знаков после запятой.")
    return value


def validate_weight_pair(gross: Decimal | None, net: Decimal | None) -> None:
    """Вес брутто не может быть меньше веса нетто (только когда заданы оба)."""
    if gross is not None and net is not None and gross < net:
        raise ValueError("Вес брутто не может быть меньше веса нетто.")


def part_export_data(part: PartType, number: str | None = None) -> dict:
    """Данные детали для строк экспорта + предупреждения о недостающих полях.

    `number` — ТОЧНЫЙ артикул проданной детали (снимок действия); он идёт в
    колонку B без изменений. Замены/источник цены номер НЕ подменяют. Если
    number не передан, берётся основной номер детали (НЕ аналог).
    K (стоимость за шт) - оптовая цена каталога в USD от эффективного источника
    цены. Рублёвые цены в таможенную форму не подмешиваются.
    """
    customs = read_customs(part)  # read-only: экспорт/отчёт не пишут в базу
    if not number:
        number = identity_number(part)
    english_name = customs.customs_name_en.strip()
    name_ru = customs.customs_name_ru.strip()
    # Customs facts are explicit operator input. Neither current catalog
    # wholesale nor a manufacturer default may masquerade as historical truth.
    usd_price = customs.customs_unit_price_usd
    manufacturer = customs.manufacturer.strip().upper()
    country = resolve_customs_country(part, customs.country_of_origin, number)
    # Область применения: приоритет 1) ручное значение карточки, 2) автоопределение
    # по PartCompatibility, 3) пусто. Легаси-хардкод «МОТО ЗАПЧАСТИ» (старый
    # default модели) считается «не заполнено» и в форму не попадает никогда.
    manual_application = (customs.application_area or "").strip()
    if manual_application and manual_application.upper() != LEGACY_APPLICATION:
        application_area = manual_application.upper()
        application_source = "manual"
    else:
        application_area = resolve_customs_application(part)
        application_source = "compatibility" if application_area else "none"
    warnings = []
    if not customs.customs_name_ru.strip():
        warnings.append("не заполнено русское название")
    if not customs.customs_name_ru_confirmed:
        warnings.append("русское название не подтверждено")
    if not english_name:
        warnings.append("не заполнено английское название")
    if customs.gross_weight_kg is None:
        warnings.append("нет веса брутто")
    if customs.net_weight_kg is None:
        warnings.append("нет веса нетто")
    if usd_price is None:
        warnings.append("нет таможенной цены в USD")
    if not country:
        warnings.append("не заполнена страна производства")
    if not application_area:
        warnings.append("не определена область применения")
    # Источник веса для UI (Layer 33.1): автоматического источника весов в
    # архитектуре нет (ни BRP, ни Polaris каталог вес не хранят) - только
    # ручной ввод, при желании с проверенной ссылкой/примечанием
    # (weight_source_url/note - их смысл не меняется, здесь только читаем).
    source_note = customs.weight_source_note.strip()
    if customs.gross_weight_kg is None and customs.net_weight_kg is None:
        weight_source = "none"
    elif customs.weight_source_url.strip() or (
        source_note and source_note != MANUAL_WEIGHT_NOTE
    ):
        weight_source = "sourced"
    else:
        weight_source = "manual"
    # Готовность именно к таможенному экспорту (Layer 33.1): ровно эти три
    # поля. Цена и название сюда не входят - у них своя строка выше.
    customs_missing_reasons = []
    if not application_area:
        customs_missing_reasons.append("Не заполнена область применения")
    if customs.gross_weight_kg is None:
        customs_missing_reasons.append("Не заполнен вес брутто")
    if customs.net_weight_kg is None:
        customs_missing_reasons.append("Не заполнен вес нетто")
    if not customs.customs_name_ru_confirmed:
        customs_missing_reasons.append("Русское название не подтверждено")
    return {
        "part": part,
        "customs": customs,
        "number": number,
        "name_ru": name_ru.upper(),
        "name_ru_confirmed": customs.customs_name_ru_confirmed,
        "name_en": english_name.upper(),
        "manufacturer": manufacturer,
        "country": country,
        # The row carries both the remembered fact and its customs-form
        # representation.  CustomsOrderLine must freeze the former; the
        # 30-gram minimum belongs solely to the workbook writer.
        "actual_gross_weight_kg": customs.gross_weight_kg,
        "actual_net_weight_kg": customs.net_weight_kg,
        "gross_weight_kg": customs_export_weight_kg(customs.gross_weight_kg),
        "net_weight_kg": customs_export_weight_kg(customs.net_weight_kg),
        "usd_price": usd_price,
        "application_area": application_area,
        "application_source": application_source,
        "weight_source": weight_source,
        "customs_ready": not customs_missing_reasons,
        "customs_missing_reasons": customs_missing_reasons,
        "warnings": warnings,
    }


def excel_safe_text(value) -> str | None:
    """Текст, безопасный для openpyxl и Excel (None -> пустая ячейка).

    Убирает управляющие символы (иначе openpyxl бросает IllegalCharacterError),
    режет по лимиту Excel и нейтрализует formula injection: строка, начинающаяся
    с =, +, - или @, экранируется апострофом и остаётся ТЕКСТОМ. Кириллица,
    пробелы, дефисы внутри и артикулы не искажаются.
    """
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

    if value is None:
        return None
    text = str(value)
    text = ILLEGAL_CHARACTERS_RE.sub("", text)
    if not text:
        return None
    if len(text) > _EXCEL_MAX_TEXT:
        text = text[:_EXCEL_MAX_TEXT]
    if text.startswith(_EXCEL_FORMULA_PREFIXES):
        text = "'" + text
    return text


def build_export_rows(actions) -> list[dict]:
    """Export active consumption grouped by manufacturer and exact snapshot.

    Ordinary actions remain positive. Repair returns reduce only repair issues,
    never sales/reserves of the same part. Net repair consumption is clamped at
    zero for legacy/incomplete action history.
    """
    grouped: dict[tuple[str, str], dict] = {}
    for action in actions:
        if action.status == WarehouseAction.Status.CANCELLED:
            continue
        number = action.part_number
        if not number or number == NO_EXACT_NUMBER:
            number = part_exact_number(action.part_type, default="")
        manufacturer = action.manufacturer_name or _manufacturer_snapshot(action.part_type)
        key = (manufacturer, number)
        row = grouped.setdefault(
            key,
            {
                "action": action,
                "manufacturer_snapshot": manufacturer,
                "number_snapshot": number,
                "ordinary_quantity": Decimal("0"),
                "repair_issued_quantity": Decimal("0"),
                "repair_returned_quantity": Decimal("0"),
            },
        )
        if action.action_type == WarehouseAction.Type.REPAIR:
            row["repair_issued_quantity"] += action.quantity
        elif action.action_type == WarehouseAction.Type.REPAIR_RETURN:
            row["repair_returned_quantity"] += action.quantity
        else:
            row["ordinary_quantity"] += action.quantity

    rows = []
    for group in grouped.values():
        repair_net = max(
            group["repair_issued_quantity"] - group["repair_returned_quantity"],
            Decimal("0"),
        )
        quantity = group["ordinary_quantity"] + repair_net
        if quantity <= 0:
            continue
        row = part_export_data(
            group["action"].part_type,
            number=group["number_snapshot"],
        )
        if group["manufacturer_snapshot"]:
            row["manufacturer"] = group["manufacturer_snapshot"].upper()
        row["quantity"] = quantity
        rows.append(row)
    return sorted(rows, key=lambda row: (row["number"], row["manufacturer"]))


def _customs_row_from_version(
    part: PartType, version, quantity: Decimal, *, customs=None, number: str | None = None
) -> dict:
    """Одна строка Excel: сохранённая версия + подтверждённые каталожные факты.

    Сохранённый ввод оператора имеет приоритет. Пустые поля заполняются из
    загруженных каталогов поставщика (утверждённый контракт): страна BRP по
    правилу компании, EN-название/производитель/USD - по exact-связи или
    exact-артикулу, RU - словарём от EN. Сами версии не переписываются:
    подстановка живёт только в строке выгрузки.
    """
    if number is None:
        number = part_exact_number(part, default="")
    if version is None:
        actual_gross_weight_kg = None
        actual_net_weight_kg = None
        values = {"name_ru": "", "name_en": "", "manufacturer": "", "country": "",
                  "gross_weight_kg": None, "net_weight_kg": None, "usd_price": None,
                  "application_area": "", "source_reference": "", "name_ru_confirmed": False}
    else:
        actual_gross_weight_kg = version.gross_weight_kg
        actual_net_weight_kg = version.net_weight_kg
        application = (version.application_area or "").strip().upper()
        if application == LEGACY_APPLICATION:
            application = ""  # легаси-хардкод считается «не заполнено»
        if not application:
            # Автоопределение по PartCompatibility (Слой 33.1) сохраняется: это
            # не каталог поставщика, а подтверждённая пользователем
            # совместимость с техникой. Ценой, страной и названиями подменять
            # ничего нельзя, а область применения так работала и раньше.
            application = resolve_customs_application(part)
        values = {
            "name_ru": version.customs_name_ru.strip().upper(),
            "name_en": version.customs_name_en.strip().upper(),
            "manufacturer": version.manufacturer.strip().upper(),
            "country": version.country_of_origin.strip().upper(),
            "gross_weight_kg": customs_export_weight_kg(version.gross_weight_kg),
            "net_weight_kg": customs_export_weight_kg(version.net_weight_kg),
            "usd_price": version.customs_unit_price_usd,
            "application_area": application,
            "source_reference": version.source_reference,
            "name_ru_confirmed": version.customs_name_ru_confirmed,
        }
    values["country"] = resolve_customs_country(part, values["country"], number)
    # Каталог дополняет только то, чего оператор не сохранил. Поля без
    # подтверждённого источника (трекинг, веса, область применения, страна
    # не-BRP брендов) остаются пустыми - их дозаполняет сотрудник в Excel.
    if not values["name_en"]:
        values["name_en"] = catalog_english_name(part, number)
    if not values["name_ru"] and values["name_en"]:
        values["name_ru"] = auto_customs_name_ru(values["name_en"])
    # Обычная таможенная выгрузка - текущая операторская форма. Снимок версии
    # продолжает задавать исторические технические поля, но явно сохранённое
    # общее русское имя карточки всегда сильнее старой версии. Frozen
    # CustomsOrderLine сюда не попадает и остаётся неизменяемым.
    current_customs = read_customs(part)
    current_name = current_customs.customs_name_ru.strip()
    if current_name and current_customs.customs_name_ru_confirmed:
        values["name_ru"] = current_name.upper()
        values["name_ru_confirmed"] = True
    if not values["manufacturer"]:
        values["manufacturer"] = catalog_manufacturer_name(part, number)
    if values["usd_price"] is None:
        values["usd_price"] = catalog_customs_usd(part, number)
    missing = [label for key, label in (
        ("name_ru", "не заполнено русское название"),
        ("name_ru_confirmed", "русское название не подтверждено"),
        ("name_en", "не заполнено английское название"),
        ("manufacturer", "не заполнен производитель"),
        ("country", "не заполнена страна производства"),
        ("gross_weight_kg", "нет веса брутто"),
        ("net_weight_kg", "нет веса нетто"),
        ("usd_price", "нет таможенной цены в USD"),
        ("application_area", "не определена область применения"),
    ) if not values[key]]
    return {
        "part": part,
        "customs": customs if customs is not None else read_customs(part),
        "number": number,
        "quantity": quantity,
        "version": version,
        "version_number": version.version if version is not None else None,
        "application_source": _application_source(version, values["application_area"]),
        "weight_source": "customs_version" if version is not None else "none",
        # «Данные заведены» и «данные полны» - разные вопросы, и ни один из
        # них выгрузку не отменяет: незаполненное поле уходит в Excel пустым,
        # а сама операция остаётся строкой. Обе величины нужны отчёту, чтобы
        # назвать оператору число позиций, которые ему предстоит дозаполнить.
        "customs_entered": version is not None,
        "customs_ready": not missing,
        "customs_missing_reasons": missing,
        "warnings": missing, **values,
        "actual_gross_weight_kg": actual_gross_weight_kg,
        "actual_net_weight_kg": actual_net_weight_kg,
    }


def _application_source(version, application_area: str) -> str:
    if not application_area:
        return "none"
    if version is not None and (version.application_area or "").strip():
        return "manual"
    return "compatibility"


def _customs_versions_by_part(part_ids) -> dict[int, list]:
    """Все сохранённые версии запрошенных деталей одним запросом."""
    versions: dict[int, list] = {part_id: [] for part_id in part_ids}
    saved = PartCustomsDataVersion.objects.filter(part_type_id__in=part_ids).order_by(
        "part_type_id", "effective_from", "version"
    )
    for version in saved:
        versions[version.part_type_id].append(version)
    return versions


def _version_at(versions: list, at):
    """Версия, действовавшая в момент движения.

    Первая версия намеренно покрывает и более ранние движения: пользователь
    заполняет таможенную карточку уже после того, как деталь появилась.
    Последующие версии никогда не переписывают более раннее движение.
    """
    if not versions:
        return None
    effective = [version for version in versions if version.effective_from <= at]
    return effective[-1] if effective else versions[0]


def customs_data_version_for(part: PartType, at):
    """Версия таможенных данных детали на момент ``at``."""
    return _version_at(
        list(part.customs_data_versions.order_by("effective_from", "version")), at
    )


def _customs_rows_from_lines(lines) -> list[dict]:
    """Свернуть канонические строки в строки Excel.

    Ключ строки: деталь, версия таможенных данных и доказанный артикул.
    Разные версии и разные артикулы одной детали остаются разными строками:
    склеив их, выгрузка выдала бы за один товар два разных исторических факта.
    """
    parts = {}
    versions = {}
    totals: dict[tuple, Decimal] = {}
    for line in lines:
        if line["quantity"] <= 0:
            continue  # полностью возвращённая строка расхода не образует
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
        row["provenance"] = SALES_REPAIRS_PROVENANCE
        row["source_key"] = key
        rows.append(row)
    # Артикул у нескольких строк может быть пустым (историческое происхождение
    # не доказано). Тогда порядок задают название и деталь, иначе строки
    # выстраивались бы произвольно и файл менялся бы от выгрузки к выгрузке.
    return sorted(
        rows,
        key=lambda row: (
            row["number"], row["name_ru"], row["name_en"],
            row["source_key"][0], row["version_number"] or 0,
        ),
    )


def historical_customs_rows(
    *, date_from=None, date_to=None, action_type="", q="", part_number="", location_code="",
    unassigned_only=False,
) -> list[dict]:
    """Исторический таможенный расход по сохранённым профилям деталей.

    Источник - те же канонические строки документов, что и в отчёте «Продажи и
    ремонты»: строки проведённых продаж и проведённых ремонтов с действующим
    количеством. Приёмки, перемещения, корректировки и списания сюда не входят:
    клиенту эти детали не уходили.

    Сохранённая страна имеет приоритет; пустая страна BRP заполняется
    утверждённым правилом компании. Остальные незаполненные поля остаются
    пустыми, но саму операцию из выгрузки не вычёркивают.
    """
    filters = {
        "date_from": date_from, "date_to": date_to, "action_type": action_type,
        "q": q, "part_number": part_number, "location_code": location_code,
        "unassigned_only": unassigned_only,
    }
    sales_rows = _customs_rows_from_lines(
        [line for line in canonical_customs_lines(
            date_from=date_from, date_to=date_to, action_type=action_type, q=q,
            part_number=part_number, location_code=location_code,
            unassigned_only=unassigned_only,
        ) if not line.get("is_analog")]
    )
    return sales_rows + ordered_customs_rows(**filters)


def ordered_customs_rows(**filters) -> list[dict]:
    from apps.ordered_parts.customs import ordered_parts_customs_rows

    if filters.get("action_type") or filters.get("location_code"):
        return []
    return ordered_parts_customs_rows(
        date_from=filters.get("date_from"), date_to=filters.get("date_to"),
        q=filters.get("q", ""), part_number=filters.get("part_number", ""),
    )


def historical_analog_customs_rows(
    *, date_from=None, date_to=None, action_type="", q="", part_number="", location_code="",
    unassigned_only=False,
) -> list[dict]:
    """Исторический таможенный расход только явно связанных аналогов."""
    return _customs_rows_from_lines(
        [line for line in canonical_customs_lines(
            date_from=date_from, date_to=date_to, action_type=action_type, q=q,
            part_number=part_number, location_code=location_code,
            unassigned_only=unassigned_only,
        ) if line.get("is_analog")]
    )


def _report_all_time_totals() -> dict:
    """Итоги «Продаж и ремонтов» за всё время: независимый ориентир сверки.

    Считает сам отчёт, а не таможенный код: иначе сверка сравнивала бы одну и
    ту же реализацию сама с собой и ничего бы не доказывала.
    """
    from apps.reports.services import Period, get_clients_sales_and_repairs

    rows = get_clients_sales_and_repairs(Period(None, None, "all"))
    return {
        "quantity": sum(
            (row["sale_quantity"] + row["repair_quantity"] for row in rows), Decimal("0")
        ),
        "amount": money(
            sum((row["client_total_known"] for row in rows), Decimal("0"))
        ),
        "customers": len(rows),
        "customers_with_unknown_price": sum(
            1 for row in rows if row["client_total_unknown"]
        ),
    }


def customs_export_reconciliation(
    *, date_from=None, date_to=None, action_type="", q="", part_number="", location_code="",
) -> dict:
    """Read-only сверка канонических строк расхода и строк таможенной выгрузки.

    Проверяется главное: агрегирование не теряет и не удваивает расход. Каждая
    каноническая строка обязана иметь свою строку XLSX, а сумма количеств до и
    после свёртки обязана совпасть.

    Неполные таможенные данные выгрузку больше НЕ блокируют — это решение
    продукта: оператор дозаполняет пустые ячейки в самом Excel. Здесь они
    остаются видимой величиной (``incomplete``), чтобы предупреждение в
    интерфейсе считалось, а не задавалось руками.
    """
    filters = {
        "date_from": date_from, "date_to": date_to, "action_type": action_type,
        "q": q, "part_number": part_number, "location_code": location_code,
    }
    lines = canonical_customs_lines(**filters)
    rows = _customs_rows_from_lines(lines)
    rows_by_key = {row["source_key"]: row for row in rows}

    effective = [line for line in lines if line["quantity"] > 0]
    fully_returned = [line for line in lines if line["quantity"] <= 0]
    silent, incomplete, article_unproven, price_unknown = [], [], [], []
    for line in effective:
        version = line["version"]
        key = (
            line["part_id"], version.pk if version is not None else None, line["number"]
        )
        row = rows_by_key.get(key)
        if row is None:
            silent.append(line)
            continue
        if not row["customs_ready"]:
            incomplete.append(line)
        if line["article_status"] != ARTICLE_PROVEN:
            article_unproven.append(line)
        if not line["amount_known"]:
            price_unknown.append(line)

    quantity = sum((line["quantity"] for line in effective), Decimal("0"))
    row_quantity = sum((row["quantity"] for row in rows), Decimal("0"))
    amount = money(
        sum(
            (line["amount"] for line in effective if line["amount_known"]),
            Decimal("0"),
        )
    )
    seen = set()
    duplicates = []
    for line in lines:
        marker = (line["kind"], line["line_id"])
        if marker in seen:
            duplicates.append(marker)
        seen.add(marker)

    all_time = not (date_from or date_to or action_type or q or part_number or location_code)
    report = _report_all_time_totals() if all_time else None
    return {
        "lines": lines,
        "rows": rows,
        "effective": effective,
        "fully_returned": fully_returned,
        "incomplete": incomplete,
        "incomplete_rows": [row for row in rows if not row["customs_ready"]],
        "article_unproven": article_unproven,
        "price_unknown": price_unknown,
        "silent": silent,
        "duplicates": duplicates,
        "totals": {
            "line_count": len(lines),
            "effective_line_count": len(effective),
            "row_count": len(rows),
            "quantity": quantity,
            "row_quantity": row_quantity,
            "amount": amount,
        },
        "report": report,
        "delta": None if report is None else {
            "quantity": quantity - report["quantity"],
            "amount": money(amount - report["amount"]),
        },
    }



def _center_data_row(sheet, row: int) -> None:
    """Единое оформление строки данных: центр по обеим осям + перенос текста.

    Для КАЖДОЙ ячейки создаётся свой Alignment: общий mutable-объект openpyxl
    разделял бы стиль между ячейками. Высота строки сбрасывается в авто, иначе
    строки шаблона с зафиксированной высотой (15/18) визуально «съезжают»
    относительно соседних.
    """
    from copy import copy

    from openpyxl.cell.cell import MergedCell
    from openpyxl.styles import Alignment

    canonical_font = copy(sheet["D10"].font)
    for column in TEMPLATE_DATA_COLUMNS:
        cell = sheet[f"{column}{row}"]
        if isinstance(cell, MergedCell):
            continue
        cell.font = copy(canonical_font)
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True, shrink_to_fit=False
        )
    text = str(sheet[f"C{row}"].value or "")
    sheet.row_dimensions[row].height = 30 if len(text) > 34 else None


ORDERED_ARTICLE_FILL_RGB = "FFC6EFCE"


def _mark_ordered_article(sheet, row: int) -> None:
    from openpyxl.styles import PatternFill

    sheet[f"B{row}"].fill = PatternFill(
        fill_type="solid", start_color=ORDERED_ARTICLE_FILL_RGB,
        end_color=ORDERED_ARTICLE_FILL_RGB,
    )


def export_customs_xlsx(actions=None, *, rows=None, sheet_rows=None) -> BytesIO:
    """Заполнить копию шаблона «Форма для заказа» отфильтрованными действиями.

    Шаблон: лист «Лист1», строки 1-9 (инструкции/шапка) сохраняются. Товарный
    диапазон (строки 10..150) — заготовка шаблона с предзаполненными BRP,
    CANADA, СНЕГОХОД и формулами: перед записью значения очищаются, чтобы
    ниже последней реальной позиции не осталось ложных товарных данных и
    ложных «0,00». Стили, границы, заливка и ширины колонок сохраняются.

    Формулы I (=J*G) и L (=K*J) проставляются только на фактических строках и
    ссылаются на свою строку. Текстовые поля — в ВЕРХНЕМ регистре через
    санитайзер. Пустые веса и отсутствующая оптовая цена остаются пустыми:
    ничего не выдумывается.
    """
    from copy import deepcopy

    import openpyxl

    if sheet_rows is not None and (rows is not None or actions is not None):
        raise ValueError("Передайте строки одного листа или именованные листы.")
    if sheet_rows is None and rows is None:
        rows = build_export_rows(actions or [])
    if not TEMPLATE_PATH.exists():  # явная причина вместо голого FileNotFoundError
        raise ActionError(
            f"Шаблон таможенной формы не найден: {TEMPLATE_PATH}. "
            "Он должен поставляться вместе с приложением."
        )
    workbook = openpyxl.load_workbook(str(TEMPLATE_PATH))
    template = workbook[TEMPLATE_SHEET]
    if sheet_rows is None:
        sheets = [(template, rows)]
    else:
        sheet_rows = list(sheet_rows)
        if not sheet_rows:
            raise ValueError("Для выгрузки нужен хотя бы один лист.")
        sheets = []
        # Копируем чистый шаблон до заполнения любого листа. copy_worksheet
        # сохраняет ячейки, стили, объединения и размеры, но не изображения
        # и настройки печати/просмотра: переносим их отдельно.
        for title, data in sheet_rows[1:]:
            sheet = workbook.copy_worksheet(template)
            sheet.title = title
            sheet.print_area = template.print_area
            sheet.print_title_rows = template.print_title_rows
            sheet.print_title_cols = template.print_title_cols
            for attribute in (
                "views", "HeaderFooter", "auto_filter", "data_validations",
                "conditional_formatting", "row_breaks", "col_breaks", "protection",
            ):
                setattr(sheet, attribute, deepcopy(getattr(template, attribute)))
            for picture in template._images:
                sheet.add_image(deepcopy(picture))
            for chart in template._charts:
                sheet.add_chart(deepcopy(chart))
            sheets.append((sheet, data))
        template.title = sheet_rows[0][0]
        sheets.insert(0, (template, sheet_rows[0][1]))

    for sheet, data in sheets:
        _fill_customs_sheet(sheet, data)

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer


def _fill_customs_sheet(sheet, rows) -> None:
    """Единый формат данных для обычного экспорта и сохранённых заказов."""

    # Раздвинуть шаблон ПЕРЕД строкой итога: ни одна историческая строка не
    # имеет права затереть итог или пропасть за пределами исходных 140 строк.
    capacity = TEMPLATE_DATA_END_ROW - TEMPLATE_DATA_START_ROW + 1
    if len(rows) > capacity:
        from copy import copy

        extra = len(rows) - capacity
        totals_row = TEMPLATE_DATA_END_ROW + 1
        sheet.insert_rows(totals_row, extra)
        for target_row in range(totals_row, totals_row + extra):
            for column in TEMPLATE_DATA_COLUMNS:
                source = sheet[f"{column}{TEMPLATE_DATA_END_ROW}"]
                target = sheet[f"{column}{target_row}"]
                target._style = copy(source._style)
                target.number_format = source.number_format
        # openpyxl НЕ двигает объединённые диапазоны при вставке строк: без
        # этого подпись итога осталась бы поверх строки данных и склеила бы
        # три её колонки (в том числе вес нетто).
        for merged in list(sheet.merged_cells.ranges):
            if merged.min_row >= totals_row:
                sheet.unmerge_cells(str(merged))
                sheet.merge_cells(
                    start_row=merged.min_row + extra, start_column=merged.min_col,
                    end_row=merged.max_row + extra, end_column=merged.max_col,
                )

    data_end_row = max(TEMPLATE_DATA_END_ROW, TEMPLATE_DATA_START_ROW + len(rows) - 1)
    # Очистка ТОЛЬКО значений товарного диапазона (стили/границы остаются).
    # Объединённые ячейки шаблона пропускаем: у них value read-only.
    from openpyxl.cell.cell import MergedCell

    for r in range(TEMPLATE_DATA_START_ROW, data_end_row + 1):
        for column in TEMPLATE_DATA_COLUMNS:
            cell = sheet[f"{column}{r}"]
            if not isinstance(cell, MergedCell):
                cell.value = None

    for offset, row in enumerate(rows):
        r = TEMPLATE_DATA_START_ROW + offset
        sheet[f"A{r}"] = None  # номер трекинга заполняется вручную
        # Текстовые колонки — только через санитайзер: управляющие символы из
        # прайсов ломали workbook, а «=»/«+»/«-»/«@» превращались в формулу.
        sheet[f"B{r}"] = excel_safe_text(row["number"])
        sheet[f"C{r}"] = excel_safe_text(row["name_ru"])
        sheet[f"D{r}"] = excel_safe_text(row["name_en"])
        sheet[f"E{r}"] = excel_safe_text(row["manufacturer"])
        sheet[f"F{r}"] = excel_safe_text(row["country"])
        # A frozen order contains the actual remembered kg values.  The
        # customs minimum is deliberately applied here, at the export edge,
        # so a 12 g part never becomes a fictitious 30 g part in storage.
        sheet[f"G{r}"] = customs_export_weight_kg(row["gross_weight_kg"])
        sheet[f"H{r}"] = customs_export_weight_kg(row["net_weight_kg"])
        sheet[f"I{r}"] = f"=J{r}*G{r}"  # вес брутто сумма = брутто/шт * количество
        for column in "GHI":
            sheet[f"{column}{r}"].number_format = "0.00"
        sheet[f"J{r}"] = row["quantity"]  # openpyxl пишет Decimal как число
        sheet[f"K{r}"] = row["usd_price"]  # оптовая цена прайса в USD
        sheet[f"L{r}"] = f"=K{r}*J{r}"
        sheet[f"M{r}"] = excel_safe_text(row["application_area"])
        _center_data_row(sheet, r)  # включая последнюю строку
        if row.get("provenance") == ORDERED_PROVENANCE:
            _mark_ordered_article(sheet, r)

    # Итог по весу брутто. Диапазон начинается там же, где в самом шаблоне
    # (строка 7), иначе после раздвижки суммировалась бы часть строк.
    sheet[f"I{data_end_row + 1}"] = f"=SUM(I7:I{data_end_row})"
