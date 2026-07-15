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

from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import (
    PartNumber,
    PartType,
    VehicleType,
    normalize_number,
)
from apps.core.part_lookup import lookup_part_by_id, resolve_part_lookup
from apps.counting.services import find_brp_price_source
from apps.inventory.models import PartItem, StockLot
from apps.inventory.services import return_stock_lot_quantity
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
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
)

from .models import PartCustomsInfo, WarehouseAction

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
# Страна производства для этого таможенного экспорта — всегда латиницей.
CUSTOMS_COUNTRY = "CANADA"

# openpyxl запрещает управляющие символы; текст, начинающийся с этих символов,
# Excel исполняет как формулу (formula injection).
_EXCEL_FORMULA_PREFIXES = ("=", "+", "-", "@")
_EXCEL_MAX_TEXT = 32767

NOT_FOUND_MESSAGE = "Деталь не найдена в остатках склада."
MULTI_LOCATION_MESSAGE = "Деталь найдена в нескольких ячейках. Выберите, откуда списать."
NOT_ENOUGH_MESSAGE = "Недостаточно доступного остатка в выбранной ячейке."


class ActionError(Exception):
    """Действие со склада невозможно (валидация/доступность)."""


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
            PartNumber.objects.filter(part=part, normalized_value=norm)
            .order_by("-is_primary", "pk")
            .first()
        )
        if matched is not None:
            return matched.value
    primary = (
        PartNumber.objects.filter(part=part).order_by("-is_primary", "pk").first()
    )
    return primary.value if primary else (scanned_raw or "").strip()


def _price_source_number(part: PartType) -> str:
    """Catalog number that supplied price when it differs from identity."""
    brp = _brp_part_for(part)
    if brp is not None:
        if brp.retail_price_usd and brp.retail_price_usd > 0:
            return ""
        source = find_brp_price_source(brp.material_no_norm, brp)
        if source is not None and source.pk != brp.pk:
            return source.material_no
        return ""
    polaris = _polaris_part_for(part)
    if polaris is not None:
        if polaris.retail_price_usd and polaris.retail_price_usd > 0:
            return ""
        source = find_polaris_price_source(polaris.part_number_norm, polaris)
        if source is not None and source.pk != polaris.pk:
            return source.part_number
    return ""


def _manufacturer_snapshot(part: PartType) -> str:
    if _polaris_part_for(part) is not None:
        return "POLARIS"
    if _brp_part_for(part) is not None:
        return "BRP"
    return part.manufacturer.name if part.manufacturer else ""


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
        reserved = active_reserved_for_lot(lot)
        row["physical"] += lot.quantity
        row["reserved"] += reserved
        row["available"] += lot.quantity - reserved
        row["lots"].append(lot)
    locations = sorted(by_location.values(), key=lambda row: row["location"].code)
    unit_items = PartItem.objects.filter(
        part_type=part, status=PartItem.Status.AVAILABLE
    ).count()
    return {
        "part": candidate.part,
        "lookup": candidate,
        "locations": locations,
        "total_available": sum((row["available"] for row in locations), Decimal("0")),
        "unit_items": unit_items,
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
    try:
        quantity = Decimal(str(quantity).replace(",", "."))
    except (InvalidOperation, TypeError) as exc:
        raise ActionError("Некорректное количество.") from exc
    if quantity <= 0:
        raise ActionError("Количество должно быть больше нуля.")
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

    unit_price = part.recommended_price or Decimal("0")
    sale = reservation = repair_order = None
    try:
        if action_type == WarehouseAction.Type.SALE:
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
        unit_price_rub=unit_price,
        total_price_rub=money(unit_price * quantity),
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

    for line in sale.lines.select_related("stock_lot", "batch_line").all():
        if line.stock_lot_id is None:
            continue  # поштучные экземпляры сканером не продаются
        return_stock_lot_quantity(
            line.batch_line,
            action.location,
            line.quantity,
            unit_cost_rub=line.unit_cost_rub,
            restock_status=StockLot.Status.AVAILABLE,
            by=by,
            document_id=sale.pk,
            comment=f"Отмена продажи {sale.number}: {reason}"[:255],
        )

    sale.status = Sale.Status.VOIDED
    sale.canceled_at = timezone.now()
    sale.save(update_fields=["status", "canceled_at", "updated_at"])

    action.status = WarehouseAction.Status.CANCELLED
    action.cancelled_at = timezone.now()
    action.cancelled_by = by
    action.cancel_reason = reason
    action.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason"])
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
    qs = WarehouseAction.objects.select_related(
        "part_type", "location", "created_by", "cancelled_by",
        "sale", "reservation", "repair_order", "stock_return",
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
        qs = qs.filter(
            Q(location_code__icontains=location_code) | Q(location__code__icontains=location_code)
        )
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
}


def auto_customs_name_ru(english_name: str) -> str:
    """Простой пословный RU-перевод английского названия (верхний регистр)."""
    words = (english_name or "").upper().split()
    translated = []
    for word in words:
        translated.append(RU_WORDS.get(word) or RU_WORDS.get(word.strip(".,")) or word)
    return " ".join(translated).strip()


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
        BrpCatalogPart.objects.filter(related, wholesale_price_usd__gt=0)
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


def validate_weight_pair(gross: Decimal | None, net: Decimal | None) -> None:
    """Вес брутто не может быть меньше веса нетто (только когда заданы оба)."""
    if gross is not None and net is not None and gross < net:
        raise ValueError("Вес брутто не может быть меньше веса нетто.")


def part_export_data(part: PartType, number: str | None = None) -> dict:
    """Данные детали для строк экспорта + предупреждения о недостающих полях.

    `number` — ТОЧНЫЙ артикул проданной детали (снимок действия); он идёт в
    колонку B без изменений. Замены/источник цены номер НЕ подменяют. Если
    number не передан, берётся основной номер детали (НЕ аналог).
    K (стоимость за шт) - розница каталога в USD от эффективного источника
    цены. Рублёвые цены в таможенную форму не подмешиваются.
    """
    customs = read_customs(part)  # read-only: экспорт/отчёт не пишут в базу
    brp = _brp_part_for(part)
    polaris = _polaris_part_for(part)
    if not number:
        number = identity_number(part)
    if polaris is not None:
        english_name = (polaris.part_name or part.name).strip()
    else:
        english_name = (brp.part_desc if brp and brp.part_desc else part.name).strip()
    name_ru = customs.customs_name_ru.strip() or auto_customs_name_ru(english_name)
    # «Стоимость за шт» — ОПТОВАЯ цена прайса в USD (без курса и наценки).
    usd_price = None
    if brp is not None:
        usd_price = _brp_wholesale_usd(brp)
    elif polaris is not None:
        usd_price = _polaris_wholesale_usd(polaris)
    if polaris is not None:
        manufacturer = (customs.manufacturer or "POLARIS").upper()
        if manufacturer == "BRP":
            manufacturer = "POLARIS"
    else:
        manufacturer = (customs.manufacturer or "BRP").upper()
    # Страна для этого таможенного экспорта всегда латиницей.
    country = CUSTOMS_COUNTRY
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
        warnings.append("русское название: автоперевод, проверьте")
    if customs.gross_weight_kg is None:
        warnings.append("нет веса брутто")
    if customs.net_weight_kg is None:
        warnings.append("нет веса нетто")
    if usd_price is None:
        warnings.append("нет оптовой цены в USD")
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
    return {
        "part": part,
        "customs": customs,
        "number": number,
        "name_ru": name_ru.upper(),
        "name_en": english_name.upper(),
        "manufacturer": manufacturer,
        "country": country,
        "gross_weight_kg": customs.gross_weight_kg,
        "net_weight_kg": customs.net_weight_kg,
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
    """Строки экспорта: группировка по ТОЧНОМУ номеру продажи, количество сумм.

    Ключ группировки - производитель + снимок part_number, а не id детали:
    BRP 123 и POLARIS 123 остаются разными строками. Отменённые действия сюда
    не попадают (их не отдаёт actions_report).
    """
    grouped: dict[tuple[str, str], dict] = {}
    for action in actions:
        number = action.part_number or identity_number(action.part_type)
        manufacturer = action.manufacturer_name or _manufacturer_snapshot(action.part_type)
        key = (manufacturer, number)
        row = grouped.get(key)
        if row is None:
            row = part_export_data(action.part_type, number=number)
            if manufacturer:
                row["manufacturer"] = manufacturer.upper()
            row["quantity"] = Decimal("0")
            grouped[key] = row
        row["quantity"] += action.quantity
    return sorted(grouped.values(), key=lambda r: (r["number"], r["manufacturer"]))


def _center_data_row(sheet, row: int) -> None:
    """Единое оформление строки данных: центр по обеим осям + перенос текста.

    Для КАЖДОЙ ячейки создаётся свой Alignment: общий mutable-объект openpyxl
    разделял бы стиль между ячейками. Высота строки сбрасывается в авто, иначе
    строки шаблона с зафиксированной высотой (15/18) визуально «съезжают»
    относительно соседних.
    """
    from openpyxl.cell.cell import MergedCell
    from openpyxl.styles import Alignment

    for column in TEMPLATE_DATA_COLUMNS:
        cell = sheet[f"{column}{row}"]
        if isinstance(cell, MergedCell):
            continue
        cell.alignment = Alignment(
            horizontal="center", vertical="center", wrap_text=True
        )
    sheet.row_dimensions[row].height = None


def export_customs_xlsx(actions) -> BytesIO:
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
    import openpyxl

    rows = build_export_rows(actions)
    if not TEMPLATE_PATH.exists():  # явная причина вместо голого FileNotFoundError
        raise ActionError(
            f"Шаблон таможенной формы не найден: {TEMPLATE_PATH}. "
            "Он должен поставляться вместе с приложением."
        )
    workbook = openpyxl.load_workbook(str(TEMPLATE_PATH))
    sheet = workbook[TEMPLATE_SHEET]

    # Очистка ТОЛЬКО значений товарного диапазона (стили/границы остаются).
    # Объединённые ячейки шаблона пропускаем: у них value read-only.
    from openpyxl.cell.cell import MergedCell

    for r in range(TEMPLATE_DATA_START_ROW, TEMPLATE_DATA_END_ROW + 1):
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
        sheet[f"G{r}"] = row["gross_weight_kg"]  # None = пусто: вес не выдумываем
        sheet[f"H{r}"] = row["net_weight_kg"]
        sheet[f"I{r}"] = f"=J{r}*G{r}"  # вес брутто сумма = брутто/шт * количество
        # 3 знака после запятой: у мелкой детали (0.125 кг) 2 знака шаблона
        # («0.00») визуально теряют точность, хотя само число не искажается.
        for column in "GHI":
            sheet[f"{column}{r}"].number_format = "0.000"
        sheet[f"J{r}"] = row["quantity"]  # openpyxl пишет Decimal как число
        sheet[f"K{r}"] = row["usd_price"]  # оптовая цена прайса в USD
        sheet[f"L{r}"] = f"=K{r}*J{r}"
        sheet[f"M{r}"] = excel_safe_text(row["application_area"])
        _center_data_row(sheet, r)  # включая последнюю строку

    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
