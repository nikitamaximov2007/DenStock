"""Layer 33 — действия со склада (сканер) и таможенный Excel-экспорт.

Ключевые гарантии: остаток меняют ТОЛЬКО существующие сервисы продаж/резервов/
ремонта (движения и брони пишутся там); больше доступного в выбранной ячейке
провести нельзя; резерв держит доступность и возвращает её при отмене; каждый
проведённый шаг оставляет журнальную запись для единого отчёта; экспорт
заполняет шаблон «Формы для заказа» без выдуманных весов и с ценой в USD от
эффективного источника BRP.
"""
from decimal import Decimal
from io import BytesIO, StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.actions.services import (
    ActionError,
    actions_report,
    auto_customs_name_ru,
    build_export_rows,
    cancel_warehouse_action,
    identity_number,
    part_export_data,
    perform_action,
    resolve_part,
    stock_overview,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, PartBarcode, PartNumber, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale
from apps.sales.services import cancel_reservation
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


def _finalized_line(sup, part, admin, *, qty, unit_cost="100"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(qty), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


def _stock(part, location, qty, sup, admin):
    line = _finalized_line(sup, part, admin, qty=str(qty))
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Вариатор")
    unit = Unit.objects.get(name="Штука")
    loc1 = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-L02-D03-C08", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка 2", code="S04-L03-D01-C01", storage_allowed=True, is_active=True
    )
    # Деталь из BRP (промоушен даёт номер и связь для USD-цены в экспорте).
    brp = BrpCatalogPart.objects.create(
        material_no="417127016", part_desc="ROLLER PULLEY",
        retail_price_usd=Decimal("25.99"), wholesale_price_usd=Decimal("20"),
    )
    roller = promote_to_warehouse(brp, by=admin)  # recommended_price 3821 (25.99*105*1.4)
    lot1 = _stock(roller, loc1, 2, sup, admin)
    lot2 = _stock(roller, loc2, 5, sup, admin)
    # Деталь в одной ячейке (для автовыбора).
    single = PartType.objects.create(
        name="Болт одноместный", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=single, value="700100", kind=PartNumber.Kind.OEM)
    PartBarcode.objects.create(part=single, value="BAR-700100")
    single_lot = _stock(single, loc1, 10, sup, admin)
    return {
        "sup": sup, "cat": cat, "unit": unit, "loc1": loc1, "loc2": loc2,
        "brp": brp, "roller": roller, "lot1": lot1, "lot2": lot2,
        "single": single, "single_lot": single_lot, "admin": admin,
    }


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


# --- Поиск по скану и обзор остатков --------------------------------------------------


def test_resolve_part_by_number_and_barcode(data):
    assert resolve_part("417127016") == data["roller"]
    assert resolve_part("417 127 016") == data["roller"]  # нормализация
    assert resolve_part("BAR-700100") == data["single"]  # штрихкод
    assert resolve_part("NO-SUCH-1") is None


def test_stock_overview_groups_by_location(data):
    overview = stock_overview(data["roller"])
    assert overview["total_available"] == Decimal("7")
    codes = [row["location"].code for row in overview["locations"]]
    assert codes == ["S01-L02-D03-C08", "S04-L03-D01-C01"]  # стабильный порядок
    first = overview["locations"][0]
    assert first["physical"] == Decimal("2")
    assert first["reserved"] == Decimal("0")
    assert first["available"] == Decimal("2")


# --- Продажа ---------------------------------------------------------------------------


def test_sale_decreases_stock_and_creates_records(data):
    movements_before = StockMovement.objects.count()
    action = perform_action(
        part=data["roller"], location=data["loc2"],
        action_type="sale", quantity="2", customer_comment="Иванов", by=data["admin"],
    )
    data["lot2"].refresh_from_db()
    assert data["lot2"].quantity == Decimal("3")  # списано из выбранной ячейки
    assert StockMovement.objects.count() > movements_before  # движение записано
    sale = action.sale
    assert sale is not None and sale.status == Sale.Status.COMPLETED
    assert sale.customer_name == "Иванов"
    assert action.action_type == "sale"
    assert action.unit_price_rub == data["roller"].recommended_price
    assert action.total_price_rub == data["roller"].recommended_price * 2
    # Ячейка-источник не тронута.
    data["lot1"].refresh_from_db()
    assert data["lot1"].quantity == Decimal("2")


def test_sale_cannot_exceed_available(data):
    with pytest.raises(ActionError, match="Недостаточно доступного остатка"):
        perform_action(
            part=data["roller"], location=data["loc1"],
            action_type="sale", quantity="3", customer_comment="Иванов", by=data["admin"],
        )
    data["lot1"].refresh_from_db()
    assert data["lot1"].quantity == Decimal("2")  # ничего не списано
    assert WarehouseAction.objects.count() == 0
    assert Sale.objects.count() == 0  # документ не остался висеть


def test_sale_splits_quantity_over_lots_fifo(data):
    # Второй лот той же детали в той же ячейке (другая строка партии).
    extra = _stock(data["roller"], data["loc1"], 3, data["sup"], data["admin"])
    action = perform_action(
        part=data["roller"], location=data["loc1"],
        action_type="sale", quantity="4", customer_comment="Пачкой", by=data["admin"],
    )
    data["lot1"].refresh_from_db()
    extra.refresh_from_db()
    assert data["lot1"].quantity == Decimal("0")  # FIFO: старый лот первым
    assert extra.quantity == Decimal("1")
    assert action.sale.lines.count() == 2  # две строки одного документа


# --- Резерв ----------------------------------------------------------------------------


def test_reserve_holds_availability_without_stock_change(data):
    action = perform_action(
        part=data["roller"], location=data["loc2"],
        action_type="reserve", quantity="4", customer_comment="Петров", by=data["admin"],
    )
    reservation = action.reservation
    assert reservation.status == Reservation.Status.ACTIVE
    data["lot2"].refresh_from_db()
    assert data["lot2"].quantity == Decimal("5")  # физически остаток цел
    overview = stock_overview(data["roller"])
    row = next(r for r in overview["locations"] if r["location"] == data["loc2"])
    assert row["reserved"] == Decimal("4")
    assert row["available"] == Decimal("1")
    # Продать больше доступного (с учётом брони) нельзя.
    with pytest.raises(ActionError):
        perform_action(
            part=data["roller"], location=data["loc2"],
            action_type="sale", quantity="2", customer_comment="Иванов", by=data["admin"],
        )
    # Отмена брони возвращает доступность.
    cancel_reservation(reservation, by=data["admin"])
    overview = stock_overview(data["roller"])
    row = next(r for r in overview["locations"] if r["location"] == data["loc2"])
    assert row["available"] == Decimal("5")


def test_cannot_reserve_more_than_available(data):
    with pytest.raises(ActionError, match="Недостаточно доступного остатка"):
        perform_action(
            part=data["roller"], location=data["loc1"],
            action_type="reserve", quantity="10", customer_comment="Петров", by=data["admin"],
        )
    assert Reservation.objects.count() == 0


# --- Ремонт ----------------------------------------------------------------------------


def test_repair_decreases_stock(data):
    action = perform_action(
        part=data["single"], location=data["loc1"],
        action_type="repair", quantity="3",
        customer_comment="Сидоров, Ski-Doo", by=data["admin"],
    )
    data["single_lot"].refresh_from_db()
    assert data["single_lot"].quantity == Decimal("7")
    order = action.repair_order
    assert order.status == RepairOrder.Status.COMPLETED
    assert order.customer_name == "Сидоров, Ski-Doo"


def test_comment_required(data):
    with pytest.raises(ActionError, match="клиента или комментарий"):
        perform_action(
            part=data["single"], location=data["loc1"],
            action_type="sale", quantity="1", customer_comment="  ", by=data["admin"],
        )


# --- Отчёт -----------------------------------------------------------------------------


def test_report_filters_and_totals(data):
    perform_action(part=data["roller"], location=data["loc2"], action_type="sale",
                   quantity="1", customer_comment="Иванов", by=data["admin"])
    perform_action(part=data["roller"], location=data["loc2"], action_type="reserve",
                   quantity="2", customer_comment="Петров", by=data["admin"])
    perform_action(part=data["single"], location=data["loc1"], action_type="repair",
                   quantity="3", customer_comment="Сидоров", by=data["admin"])
    qs, totals = actions_report()
    assert qs.count() == 3
    assert totals["quantity"] == Decimal("6")
    qs, totals = actions_report(action_type="sale")
    assert qs.count() == 1
    assert totals["value"] == data["roller"].recommended_price
    # icontains с кириллицей нечувствителен к регистру на Postgres; в тестах
    # SQLite, поэтому ищем в точном регистре.
    qs, _t = actions_report(q="Петров")
    assert qs.count() == 1
    qs, _t = actions_report(part_number="700100")
    assert qs.count() == 1
    assert qs.first().part_type == data["single"]
    qs, _t = actions_report(location_code="S04")
    assert qs.count() == 2


# --- Экраны ----------------------------------------------------------------------------


def test_scan_page_single_location_preselected(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("actions_scan") + "?q=700100").content.decode()
    assert "Болт одноместный" in html
    assert 'checked' in html  # единственная ячейка выбрана сразу
    assert "Деталь найдена в нескольких ячейках" not in html
    assert "Провести действие" in html


def test_scan_page_multiple_locations_require_choice(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("actions_scan") + "?q=417127016").content.decode()
    assert "Деталь найдена в нескольких ячейках. Выберите, откуда списать." in html
    assert "S01-L02-D03-C08" in html and "S04-L03-D01-C01" in html
    assert 'checked' not in html.split("Ячейка списания")[1].split("</table>")[0]


def test_scan_page_unknown_part(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("actions_scan") + "?q=NOPE-404").content.decode()
    assert "Деталь не найдена в остатках склада." in html


def test_perform_via_view_and_success_message(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(
        reverse("actions_perform"),
        {
            "part_id": data["single"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "1",
            "customer_comment": "Иванов", "q": "700100",
        },
        follow=True,
    )
    text = resp.content.decode()
    assert "Действие проведено: Продажа, 1 шт, S01-L02-D03-C08" in text
    data["single_lot"].refresh_from_db()
    assert data["single_lot"].quantity == Decimal("9")


def test_permissions_per_action(client, make_user, data):
    # Кладовщик: ремонт можно, продажу нельзя.
    _login(client, make_user, role=roles.STOREKEEPER, name="sklad")
    assert client.get(reverse("actions_scan")).status_code == 200
    resp = client.post(
        reverse("actions_perform"),
        {
            "part_id": data["single"].pk, "location_id": data["loc1"].pk,
            "action_type": "sale", "quantity": "1", "customer_comment": "Иванов",
        },
    )
    assert resp.status_code == 403
    client.logout()
    # Наблюдатель: страница закрыта.
    _login(client, make_user, role=roles.VIEWER, name="viewer")
    assert client.get(reverse("actions_scan")).status_code == 403


# --- Таможенные данные и экспорт --------------------------------------------------------


def test_auto_customs_name_ru():
    assert auto_customs_name_ru("HEX. FLANGED SCREW M6 X 18") == (
        "ШЕСТИГРАННЫЙ ФЛАНЦЕВЫЙ ВИНТ M6 X 18"
    )
    assert auto_customs_name_ru("ROLLER PULLEY") == "РОЛИК ШКИВ"
    assert auto_customs_name_ru("UNKNOWNWORD 42") == "UNKNOWNWORD 42"  # не выдумываем


def test_part_export_data_uses_effective_wholesale_price(data):
    # Точный номер без оптовой + замена с оптовой: цена от источника,
    # номер детали остаётся точным.
    zero = BrpCatalogPart.objects.create(
        material_no="250000059", part_desc="HEX. FLANGED SCEW M6 X 18",
        retail_price_usd=Decimal("0"), wholesale_price_usd=Decimal("0"),
    )
    BrpCatalogPart.objects.create(
        material_no="250000418", part_desc="FLANGED HEX. SCREW",
        retail_price_usd=Decimal("4.19"), wholesale_price_usd=Decimal("3.29"),
        replacement_no_1="250000059",
    )
    part = promote_to_warehouse(zero, by=data["admin"])
    row = part_export_data(part)
    assert row["number"] == "250000059"  # личность остаётся отсканированной
    assert row["usd_price"] == Decimal("3.29")  # ОПТОВАЯ от эффективного источника
    assert row["usd_price"] != Decimal("4.19")  # не розница
    assert "нет оптовой цены в USD" not in row["warnings"]
    assert "нет веса брутто" in row["warnings"]  # вес не выдуман


def test_customs_edit_saves_manual_data(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    resp = client.post(
        reverse("actions_customs_edit", args=[data["roller"].pk]),
        {
            "customs_name_ru": "Ролик вариатора",
            "gross_weight_kg": "0,25", "net_weight_kg": "0.2",
            "weight_source_url": "https://example.com/spec",
            "weight_source_note": "страница поставщика",
            "weight_verified": "on",
            "application_area": "",  # не заполнено - легаси "МОТО ЗАПЧАСТИ" больше не выбор
        },
    )
    assert resp.status_code == 302
    customs = PartCustomsInfo.objects.get(part_type=data["roller"])
    assert customs.customs_name_ru == "Ролик вариатора"
    assert customs.customs_name_source == PartCustomsInfo.NameSource.MANUAL
    assert customs.gross_weight_kg == Decimal("0.25")
    assert customs.net_weight_kg == Decimal("0.2")
    assert customs.weight_source_url == "https://example.com/spec"
    assert customs.weight_verified is True
    assert customs.application_area == ""
    # Ручное название уходит в экспорт в ВЕРХНЕМ регистре.
    row = part_export_data(data["roller"])
    assert row["name_ru"] == "РОЛИК ВАРИАТОРА"
    # Применимость у карточки не заполнена — остаётся только это предупреждение.
    assert row["warnings"] == ["не определена область применения"]


def test_export_rows_group_by_part(data):
    perform_action(part=data["roller"], location=data["loc1"], action_type="sale",
                   quantity="1", customer_comment="Иванов", by=data["admin"])
    perform_action(part=data["roller"], location=data["loc2"], action_type="sale",
                   quantity="2", customer_comment="Петров", by=data["admin"])
    qs, _t = actions_report()
    rows = build_export_rows(list(qs))
    assert len(rows) == 1  # одна деталь = одна строка
    assert rows[0]["quantity"] == Decimal("3")  # количество просуммировано


def test_export_xlsx_structure(client, make_user, data):
    import openpyxl

    perform_action(part=data["roller"], location=data["loc2"], action_type="sale",
                   quantity="2", customer_comment="Иванов", by=data["admin"])
    perform_action(part=data["single"], location=data["loc1"], action_type="repair",
                   quantity="1", customer_comment="Сидоров", by=data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200
    assert "customs_order_" in resp["Content-Disposition"]
    workbook = openpyxl.load_workbook(BytesIO(resp.content))
    sheet = workbook["Лист1"]
    # Шапка и инструкции (строки 1-9) сохранены.
    assert "ЗАПОЛНЯЕМ" in str(sheet["A1"].value or "").upper() or sheet["A1"].value
    assert sheet["B6"].value  # заголовки колонок на месте
    # Пример шаблона (271002228) не уехал как данные.
    values = [sheet[f"B{r}"].value for r in range(10, 15)]
    assert "271002228" not in [str(v) for v in values if v]
    # Данные с 10-й строки, отсортированы по номеру: 417127016 после 700100? нет:
    # сортировка по номеру строки экспорта: "417127016" < "700100" лексикографически.
    assert str(sheet["B10"].value) == "417127016"
    assert sheet["C10"].value == "РОЛИК ШКИВ"  # RU в верхнем регистре (автоперевод)
    assert sheet["D10"].value == "ROLLER PULLEY"
    assert sheet["E10"].value == "BRP"
    assert sheet["F10"].value == "CANADA"  # всегда латиницей
    assert sheet["G10"].value is None and sheet["H10"].value is None  # весов нет: пусто
    assert sheet["I10"].value == "=J10*G10"
    assert Decimal(str(sheet["J10"].value)) == Decimal("2")
    assert Decimal(str(sheet["K10"].value)) == Decimal("20")  # ОПТОВАЯ BRP в USD
    assert sheet["L10"].value == "=K10*J10"
    assert sheet["M10"].value is None  # применимость не задана — не выдумываем
    # Вторая строка: деталь без BRP-связи: номер со склада, цены USD нет.
    assert str(sheet["B11"].value) == "700100"
    assert sheet["K11"].value is None


def test_report_page_shows_warnings_and_export_button(client, make_user, data):
    perform_action(part=data["single"], location=data["loc1"], action_type="sale",
                   quantity="1", customer_comment="Иванов", by=data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("actions_report")).content.decode()
    assert "Экспорт в Excel для таможни" in html
    assert "нет веса брутто" in html
    assert "Таможенные данные" in html
    assert "Иванов" in html  # клиент/комментарий виден в отчёте
    assert "—" not in html


def test_scan_and_report_pages_no_em_dash(client, make_user, data):
    _login(client, make_user, superuser=True, name="boss")
    for url in (
        reverse("actions_scan") + "?q=417127016",
        reverse("actions_customs_edit", args=[data["roller"].pk]),
    ):
        assert "—" not in client.get(url).content.decode()


# --- Личность детали: точный номер, а не замена (identity hotfix) -------------------


@pytest.fixture
def variant_part(db, data):
    """Карточка из BRP 420931285 (OEM primary) с заменой 420931284 (ANALOG).

    Воспроизводит прод-баг: PartNumber.ordering ставит analog раньше oem, и
    старый отчёт через .numbers.first показывал 420931284 вместо 420931285.
    """
    brp = BrpCatalogPart.objects.create(
        material_no="420931285", part_desc="RIGHT PANEL",
        retail_price_usd=Decimal("12.50"), replacement_no_1="420931284",
    )
    part = promote_to_warehouse(brp, by=data["admin"])
    _stock(part, data["loc2"], 5, data["sup"], data["admin"])
    return part


def test_identity_number_prefers_primary_not_analog(variant_part):
    numbers = {n.value: n.kind for n in variant_part.numbers.all()}
    assert numbers == {"420931285": "oem", "420931284": "analog"}
    # .numbers.first вернул бы analog (баг); identity_number — primary.
    assert variant_part.numbers.first().value == "420931284"  # старый баг-источник
    assert identity_number(variant_part) == "420931285"
    # Точный отсканированный номер (в т.ч. замена) сохраняется как есть.
    assert identity_number(variant_part, "420931285") == "420931285"


def test_sale_snapshots_exact_scanned_number(variant_part, data):
    action = perform_action(
        part=variant_part, location=data["loc2"], action_type="sale",
        quantity="1", customer_comment="Клиент", scanned_number="420931285",
        by=data["admin"],
    )
    assert action.part_number == "420931285"  # НЕ 420931284
    assert action.part_name == "RIGHT PANEL"
    assert action.location_code == "S04-L03-D01-C01"


def test_report_and_customs_show_exact_number(client, make_user, variant_part, data):
    perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                   quantity="2", customer_comment="Клиент",
                   scanned_number="420931285", by=data["admin"])
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("actions_report")).content.decode()
    assert "420931285" in html
    assert "420931284" not in html  # замена не показывается как номер продажи


def test_export_writes_exact_number(variant_part, data):
    import openpyxl

    from apps.actions.services import export_customs_xlsx

    perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                   quantity="2", customer_comment="Клиент",
                   scanned_number="420931285", by=data["admin"])
    qs, _t = actions_report()
    sheet = openpyxl.load_workbook(export_customs_xlsx(qs))["Лист1"]
    b_values = [str(sheet[f"B{r}"].value) for r in range(10, 14) if sheet[f"B{r}"].value]
    assert "420931285" in b_values
    assert "420931284" not in b_values


def test_price_source_does_not_change_identity(db, data):
    zero = BrpCatalogPart.objects.create(
        material_no="250000059", part_desc="SCREW", retail_price_usd=Decimal("0"),
    )
    BrpCatalogPart.objects.create(
        material_no="250000418", part_desc="SCREW PRICED",
        retail_price_usd=Decimal("4.19"), replacement_no_1="250000059",
    )
    part = promote_to_warehouse(zero, by=data["admin"])
    _stock(part, data["loc1"], 3, data["sup"], data["admin"])
    action = perform_action(
        part=part, location=data["loc1"], action_type="sale", quantity="1",
        customer_comment="Клиент", scanned_number="250000059", by=data["admin"],
    )
    assert action.part_number == "250000059"  # личность точная
    assert action.price_source_number == "250000418"  # источник цены — аудит


# --- Отмена ошибочной продажи ---------------------------------------------------------


def test_cancel_sale_returns_stock(variant_part, data):
    action = perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                            quantity="2", customer_comment="Дубль",
                            scanned_number="420931285", by=data["admin"])
    lot = StockLot.objects.get(part_type=variant_part, location=data["loc2"])
    assert lot.quantity == Decimal("3")  # было 5, продали 2
    cancel_warehouse_action(action, by=data["admin"], reason="Дублирующая продажа")
    action.refresh_from_db()
    assert action.status == WarehouseAction.Status.CANCELLED
    assert action.cancel_reason == "Дублирующая продажа"
    assert action.cancelled_by == data["admin"]
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")  # остаток вернулся в ту же ячейку
    assert action.sale.status == Sale.Status.VOIDED


def test_cancelled_excluded_from_report_and_export(variant_part, data):
    a1 = perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                        quantity="2", customer_comment="Ошибка",
                        scanned_number="420931285", by=data["admin"])
    perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                   quantity="1", customer_comment="Верно",
                   scanned_number="420931285", by=data["admin"])
    cancel_warehouse_action(a1, by=data["admin"], reason="Дубль")
    qs, totals = actions_report()
    assert qs.count() == 1  # отменённое исключено
    assert totals["quantity"] == Decimal("1")
    rows = build_export_rows(list(qs))
    assert rows[0]["quantity"] == Decimal("1")  # в экспорт только активная
    qs_all, totals_all = actions_report(include_cancelled=True)
    assert qs_all.count() == 2
    assert totals_all["quantity"] == Decimal("1")  # аудит видит отмену, итоги её не считают


def test_cannot_cancel_twice(variant_part, data):
    action = perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                            quantity="1", customer_comment="X",
                            scanned_number="420931285", by=data["admin"])
    cancel_warehouse_action(action, by=data["admin"], reason="раз")
    with pytest.raises(ActionError, match="уже отменено"):
        cancel_warehouse_action(action, by=data["admin"], reason="два")


def test_cannot_cancel_non_sale(variant_part, data):
    action = perform_action(part=variant_part, location=data["loc2"], action_type="reserve",
                            quantity="1", customer_comment="X",
                            scanned_number="420931285", by=data["admin"])
    with pytest.raises(ActionError, match="только для продаж"):
        cancel_warehouse_action(action, by=data["admin"], reason="нет")


def test_cancel_requires_reason(variant_part, data):
    action = perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                            quantity="1", customer_comment="X",
                            scanned_number="420931285", by=data["admin"])
    with pytest.raises(ActionError, match="причину"):
        cancel_warehouse_action(action, by=data["admin"], reason="  ")


def test_cancel_via_view_gated_and_works(client, make_user, variant_part, data):
    action = perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                            quantity="2", customer_comment="Дубль",
                            scanned_number="420931285", by=data["admin"])
    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(reverse("actions_cancel", args=[action.pk])).status_code == 403
    client.logout()
    _login(client, make_user, role=roles.MANAGER, name="boss")
    assert client.get(reverse("actions_cancel", args=[action.pk])).status_code == 200
    resp = client.post(reverse("actions_cancel", args=[action.pk]),
                       {"reason": "Дублирующая продажа"}, follow=True)
    assert "Продажа отменена" in resp.content.decode()
    action.refresh_from_db()
    assert action.status == WarehouseAction.Status.CANCELLED
    lot = StockLot.objects.get(part_type=variant_part, location=data["loc2"])
    assert lot.quantity == Decimal("5")


def test_cancel_command_dry_run_and_commit(variant_part, data):
    action = perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                            quantity="2", customer_comment="Дубль",
                            scanned_number="420931285", by=data["admin"])
    out = StringIO()
    call_command("cancel_warehouse_action", action_id=action.pk,
                 reason="Дублирующая продажа", stdout=out)
    assert "DRY-RUN" in out.getvalue()
    action.refresh_from_db()
    assert action.status == WarehouseAction.Status.ACTIVE  # dry-run не тронул
    call_command("cancel_warehouse_action", action_id=action.pk,
                 reason="Дублирующая продажа", commit=True, stdout=StringIO())
    action.refresh_from_db()
    assert action.status == WarehouseAction.Status.CANCELLED
    assert StockLot.objects.get(
        part_type=variant_part, location=data["loc2"]
    ).quantity == Decimal("5")


def test_debug_command_reports_numbers(variant_part, data):
    perform_action(part=variant_part, location=data["loc2"], action_type="sale",
                   quantity="1", customer_comment="X",
                   scanned_number="420931285", by=data["admin"])
    out = StringIO()
    call_command("debug_warehouse_actions", material_no="420931285", stdout=out)
    text = out.getvalue()
    assert "primary/OEM номер карточки: '420931285'" in text
    assert "kind=analog" in text  # аналог виден в диагностике
    assert "номер в таможенном экспорте (колонка B): '420931285'" in text


@pytest.fixture
def legacy_replacement_part(db, data):
    """Карточка, где primary 420931284, но старое действие надо исправить на 420931285."""
    brp = BrpCatalogPart.objects.create(
        material_no="420931284", part_desc="OIL SEAL",
        retail_price_usd=Decimal("24.49"), replacement_no_1="420931285",
    )
    part = promote_to_warehouse(brp, by=data["admin"])
    _stock(part, data["loc2"], 5, data["sup"], data["admin"])
    return part


def test_repair_identity_snapshot_command_changes_only_snapshot(legacy_replacement_part, data):
    action = perform_action(
        part=legacy_replacement_part, location=data["loc2"], action_type="sale",
        quantity="1", customer_comment="Рома Чернушка", by=data["admin"],
    )
    lot = StockLot.objects.get(part_type=legacy_replacement_part, location=data["loc2"])
    qty_after_sale = lot.quantity
    movements_after_sale = StockMovement.objects.count()
    assert action.part_number == "420931284"  # legacy/backfill value, not actual sold number

    out = StringIO()
    call_command(
        "repair_warehouse_action_identity",
        action_id=action.pk,
        part_number="420931285",
        reason="Фактически продали 420931285",
        stdout=out,
    )
    assert "DRY-RUN" in out.getvalue()
    action.refresh_from_db()
    assert action.part_number == "420931284"

    call_command(
        "repair_warehouse_action_identity",
        action_id=action.pk,
        part_number="420931285",
        reason="Фактически продали 420931285",
        commit=True,
        stdout=StringIO(),
    )
    action.refresh_from_db()
    lot.refresh_from_db()
    assert action.part_number == "420931285"
    assert lot.quantity == qty_after_sale
    assert StockMovement.objects.count() == movements_after_sale

    rows = build_export_rows(list(actions_report()[0]))
    assert rows[0]["number"] == "420931285"
