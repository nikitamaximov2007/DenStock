"""Layer 33 — сервисы быстрых действий со склада и таможенного экспорта.

Физику склада НЕ дублируем: продажа/резерв/ремонт проводятся существующими
сервисами apps.sales и apps.repairs (движения, остатки, брони — там).
Здесь: поиск остатков по скану, раскладка количества по лотам выбранной
ячейки (FIFO), журнальная запись WarehouseAction для единого отчёта и
Excel-экспорт «Формы для заказа» (openpyxl, шаблон в docs/templates/).
"""
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Q, Sum

from apps.brp.models import BrpPartLink
from apps.catalog.models import PartBarcode, PartNumber, PartType, normalize_number
from apps.counting.services import find_brp_price_source
from apps.inventory.models import PartItem, StockLot
from apps.procurement.models import money
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
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

TEMPLATE_PATH = Path(settings.BASE_DIR) / "docs" / "templates" / "supplier_order_template.xlsx"
TEMPLATE_SHEET = "Лист1"
TEMPLATE_DATA_START_ROW = 10  # строка 10 шаблона — пример, перезаписывается данными

NOT_FOUND_MESSAGE = "Деталь не найдена в остатках склада."
MULTI_LOCATION_MESSAGE = "Деталь найдена в нескольких ячейках. Выберите, откуда списать."
NOT_ENOUGH_MESSAGE = "Недостаточно доступного остатка в выбранной ячейке."


class ActionError(Exception):
    """Действие со склада невозможно (валидация/доступность)."""


# --- Поиск детали и остатков по скану ----------------------------------------------


def resolve_part(raw: str) -> PartType | None:
    """Карточка склада по скану: номер (нормализованный) или штрихкод.

    Тот же порядок, что в пересчёте ячейки: складские номера, затем штрихкод.
    BRP-каталог здесь НЕ создаёт карточек: действия работают только с реальным
    остатком.
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    norm = normalize_number(raw)
    part_id = None
    if norm:
        part_id = (
            PartNumber.objects.filter(normalized_value=norm)
            .values_list("part_id", flat=True)
            .first()
        )
    if part_id is None:
        part_id = (
            PartBarcode.objects.filter(value__iexact=raw)
            .values_list("part_id", flat=True)
            .first()
        )
    if part_id is None:
        return None
    return PartType.objects.filter(pk=part_id).first()


def _lot_available(lot: StockLot) -> Decimal:
    return lot.quantity - active_reserved_for_lot(lot)


def stock_overview(part: PartType) -> dict:
    """Остатки детали по ячейкам: физически / зарезервировано / доступно.

    Быстрые действия работают с количественными лотами; поштучные экземпляры
    показываются числом со ссылкой на существующий флоу карточки детали.
    """
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
    locations = sorted(by_location.values(), key=lambda r: r["location"].code)
    unit_items = PartItem.objects.filter(
        part_type=part, status=PartItem.Status.AVAILABLE
    ).count()
    return {
        "part": part,
        "locations": locations,
        "total_available": sum((r["available"] for r in locations), Decimal("0")),
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


@transaction.atomic
def perform_action(
    *, part: PartType, location, action_type: str, quantity, customer_comment: str, by=None
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
        part_type=part,
        location=location,
        quantity=quantity,
        unit_price_rub=unit_price,
        total_price_rub=money(unit_price * quantity),
        customer_comment=customer_comment,
        sale=sale,
        reservation=reservation,
        repair_order=repair_order,
        created_by=by,
    )


# --- Единый отчёт действий -----------------------------------------------------------


def actions_report(
    *, date_from=None, date_to=None, action_type="", q="", part_number="", location_code=""
):
    """Отфильтрованный журнал действий + итоги (количество и сумма)."""
    qs = WarehouseAction.objects.select_related(
        "part_type", "location", "created_by", "sale", "reservation", "repair_order"
    )
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
            Q(part_type_id__in=list(part_ids))
            | Q(part_type__name__icontains=part_number)
        )
    if location_code:
        qs = qs.filter(location__code__icontains=location_code)
    totals = qs.aggregate(quantity=Sum("quantity"), value=Sum("total_price_rub"))
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


def get_or_create_customs(part: PartType) -> PartCustomsInfo:
    info, _created = PartCustomsInfo.objects.get_or_create(part_type=part)
    return info


def _brp_part_for(part: PartType):
    link = BrpPartLink.objects.filter(part=part).select_related("brp_part").first()
    return link.brp_part if link else None


def part_export_data(part: PartType) -> dict:
    """Данные детали для строк экспорта + предупреждения о недостающих полях.

    K (стоимость за шт) — розница BRP в ДОЛЛАРАХ от эффективного источника
    цены (правило 32.3.2: если у точной позиции розница 0, берётся связанная
    замена с ценой). Рублёвые цены в таможенную форму не подмешиваются.
    """
    customs = get_or_create_customs(part)
    brp = _brp_part_for(part)
    number = brp.material_no if brp else ""
    if not number:
        primary = (
            PartNumber.objects.filter(part=part).order_by("-is_primary", "pk").first()
        )
        number = primary.value if primary else ""
    english_name = (brp.part_desc if brp and brp.part_desc else part.name).strip()
    name_ru = customs.customs_name_ru.strip() or auto_customs_name_ru(english_name)
    usd_price = None
    if brp is not None:
        source = find_brp_price_source(brp.material_no_norm, brp)
        if source is not None and source.retail_price_usd and source.retail_price_usd > 0:
            usd_price = source.retail_price_usd
    warnings = []
    if not customs.customs_name_ru.strip():
        warnings.append("русское название: автоперевод, проверьте")
    if customs.gross_weight_kg is None:
        warnings.append("нет веса брутто")
    if customs.net_weight_kg is None:
        warnings.append("нет веса нетто")
    if usd_price is None:
        warnings.append("нет цены в USD")
    return {
        "part": part,
        "customs": customs,
        "number": number,
        "name_ru": name_ru.upper(),
        "name_en": english_name.upper(),
        "manufacturer": (customs.manufacturer or "BRP").upper(),
        "country": (customs.country_of_origin or "КАНАДА").upper(),
        "gross_weight_kg": customs.gross_weight_kg,
        "net_weight_kg": customs.net_weight_kg,
        "usd_price": usd_price,
        "application_area": (customs.application_area or "МОТО ЗАПЧАСТИ").upper(),
        "warnings": warnings,
    }


def build_export_rows(actions) -> list[dict]:
    """Строки экспорта: действия группируются по детали, количество суммируется."""
    grouped: dict[int, dict] = {}
    for action in actions:
        row = grouped.get(action.part_type_id)
        if row is None:
            row = part_export_data(action.part_type)
            row["quantity"] = Decimal("0")
            grouped[action.part_type_id] = row
        row["quantity"] += action.quantity
    return sorted(grouped.values(), key=lambda r: r["number"])


def export_customs_xlsx(actions) -> BytesIO:
    """Заполнить копию шаблона «Форма для заказа» отфильтрованными действиями.

    Шаблон: лист «Лист1», строки 1-9 (инструкции/шапка) сохраняются, данные
    с строки 10; строка 10 шаблона — пример (271002228) и перезаписывается.
    Формулы I (=J*G) и L (=K*J) сохраняются/проставляются построчно. Все
    текстовые поля пишутся в ВЕРХНЕМ регистре. Пустые веса остаются пустыми:
    веса не выдумываются.
    """
    import openpyxl

    rows = build_export_rows(actions)
    workbook = openpyxl.load_workbook(str(TEMPLATE_PATH))
    sheet = workbook[TEMPLATE_SHEET]
    # Пример шаблона в строке 10 очищается: он не должен уехать как данные.
    for column in "ABCDGHJK":
        sheet[f"{column}{TEMPLATE_DATA_START_ROW}"] = None
    for offset, row in enumerate(rows):
        r = TEMPLATE_DATA_START_ROW + offset
        sheet[f"A{r}"] = None  # номер трекинга заполняется вручную
        sheet[f"B{r}"] = row["number"]
        sheet[f"C{r}"] = row["name_ru"]
        sheet[f"D{r}"] = row["name_en"]
        sheet[f"E{r}"] = row["manufacturer"]
        sheet[f"F{r}"] = row["country"]
        sheet[f"G{r}"] = row["gross_weight_kg"]  # None = пусто: вес не выдумываем
        sheet[f"H{r}"] = row["net_weight_kg"]
        sheet[f"I{r}"] = f"=J{r}*G{r}"
        sheet[f"J{r}"] = row["quantity"]  # openpyxl пишет Decimal как число
        sheet[f"K{r}"] = row["usd_price"]
        sheet[f"L{r}"] = f"=K{r}*J{r}"
        sheet[f"M{r}"] = row["application_area"]
    buffer = BytesIO()
    workbook.save(buffer)
    buffer.seek(0)
    return buffer
