"""Таможня: оригиналы (импортированные BRP, PROX) против аналогов.

Правило владельца:
1. Деталь, заведённая вручную, - всегда АНАЛОГ, даже если позже ей записали
   производителя BRP или PROX.
2. Импортированная деталь с производителем BRP или PROX - ОРИГИНАЛ.
3. Всё остальное - АНАЛОГ (BRONCO, POLARIS, SPI, MOTUL, прочие, пусто).

«Ручная» определяется не по производителю и не по названию, а по
происхождению: импорт оставляет запись связи с каталогом (BrpPartLink,
PolarisPartLink, AftermarketCatalogPart, ArcticCatCatalogPart), ручное
создание - нет (см. apps.actions.services.imported_part_ids).
Один классификатор делит и обе Excel-выгрузки, и обе очереди «Отправить в
заказ», поэтому наборы строк совпадают, а каждая строка - ровно в одной группе.
"""
import tempfile
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import openpyxl
import pytest
from django.urls import reverse

from apps.actions.models import PartCustomsInfo
from apps.actions.services import (
    CUSTOMS_ANALOG,
    CUSTOMS_ORIGINAL,
    customs_export_rows,
    customs_group,
    customs_group_rows,
    historical_analog_customs_rows,
    historical_customs_rows,
    imported_part_ids,
    perform_action,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, Manufacturer
from apps.catalog.services import MANUAL_CATEGORY_NAME, create_manual_part
from apps.catalog_import.aftermarket_catalog import apply_file as apply_aftermarket_file
from apps.catalog_import.models import AftermarketCatalogPart
from apps.customs_orders.models import CustomsOrder
from apps.customs_orders.services import (
    create_customs_order_from_boundary,
    customs_sources,
    eligible_customs_sources,
    selection_payload,
)
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.warehouse.models import StorageLocation

pytestmark = pytest.mark.django_db

SHEET = "Лист1"
DATA_ROW = 10
ORIGINALS = {"BRP-A", "PROX-B"}
ANALOGS = {"BRONCO-C", "MANUAL-D", "OTHER-E", "MANUAL-BRP-F", "MANUAL-PROX-G"}
AFTERMARKET_HEADERS = [
    "Manufacturer", "Item SKU", "Manufacturer Number", "Description", "MSRP", "Dlr Cost",
]


@pytest.fixture
def env(django_user_model):
    from apps.suppliers.models import Supplier

    admin = django_user_model.objects.create_superuser(username="boss", password="parol-12345")
    supplier, _ = Supplier.objects.get_or_create(name="ООО Поставка")
    location, _ = StorageLocation.objects.get_or_create(
        code="S01-D01-C01",
        defaults={"name": "Ячейка", "storage_allowed": True, "is_active": True},
    )
    return {"admin": admin, "sup": supplier, "loc": location}


def _receive(env, part, quantity="10"):
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["loc"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])


def _card(part, *, manufacturer, gross="0.250", net="0.200"):
    return PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="ДЕТАЛЬ", customs_name_ru_confirmed=True,
        customs_name_en="PART", manufacturer=manufacturer, country_of_origin="CANADA",
        gross_weight_kg=Decimal(gross), net_weight_kg=Decimal(net),
        customs_unit_price_usd=Decimal("10.00"),
        application_area=PartCustomsInfo.ApplicationArea.SNOWMOBILE,
    )


def _sell(env, part, number):
    return perform_action(
        part=part, location=env["loc"], action_type="sale", quantity="1",
        customer_comment="Клиент", scanned_number=number, by=env["admin"],
    )


def _import_aftermarket(rows):
    """Настоящий импорт каталога аналогов (PROX, BRONCO, ...) из xlsx."""
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "priceupdate"
    sheet.append(AFTERMARKET_HEADERS)
    sheet.append([""] * len(AFTERMARKET_HEADERS))
    for brand, number in rows:
        sheet.append([brand, f"SKU-{number}", number, f"{brand} PART {number}", "20", "10"])
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "dealer.xlsx"
        book.save(path)
        apply_aftermarket_file(path)
    return {
        number: AftermarketCatalogPart.objects.get(manufacturer_number=number).part
        for _brand, number in rows
    }


def _imported_brp(env, number):
    """Настоящее продвижение позиции BRP-прайса на склад."""
    brp = BrpCatalogPart.objects.create(
        material_no=number, part_desc="BELT DRIVE", wholesale_price_usd=Decimal("28.15"),
    )
    return promote_to_warehouse(brp, by=env["admin"])


def _mixed_order(env):
    """Импортированные BRP A и PROX B - оригиналы. Аналоги: импортированный
    BRONCO C, ручная D без производителя, ручная OTHER E и ручные детали, которым
    позже записали BRP (F) и PROX (G)."""
    part_a = _imported_brp(env, "BRP-A")
    _receive(env, part_a)
    _card(part_a, manufacturer="BRP", gross="1.100", net="1.000")

    imported = _import_aftermarket([("PROX", "PROX-B"), ("BRONCO", "BRONCO-C")])
    part_b = imported["PROX-B"]
    _receive(env, part_b)
    _card(part_b, manufacturer="PROX")

    part_c = imported["BRONCO-C"]
    _receive(env, part_c)
    _card(part_c, manufacturer="BRONCO", gross="0.750", net="0.700")

    part_d = create_manual_part(name="РУЧНАЯ ДЕТАЛЬ", article="MANUAL-D", price="1000")
    _receive(env, part_d)
    _card(part_d, manufacturer="")

    part_e = create_manual_part(
        name="ДЕТАЛЬ OTHER", article="OTHER-E", price="1000", manufacturer_name="OTHER",
    )
    _receive(env, part_e)
    _card(part_e, manufacturer="OTHER")

    # Ручная деталь без производителя; позже ей выбрали BRP и в карточке, и в
    # таможенных данных.
    part_f = create_manual_part(name="РУЧНАЯ BRP", article="MANUAL-BRP-F", price="1000")
    part_f.manufacturer, _ = Manufacturer.objects.get_or_create(name="BRP")
    part_f.save(update_fields=["manufacturer"])
    _receive(env, part_f)
    _card(part_f, manufacturer="BRP")

    part_g = create_manual_part(
        name="РУЧНАЯ PROX", article="MANUAL-PROX-G", price="1000", manufacturer_name="PROX",
    )
    _receive(env, part_g)
    _card(part_g, manufacturer="PROX")

    for part, number in (
        (part_a, "BRP-A"), (part_b, "PROX-B"), (part_c, "BRONCO-C"),
        (part_d, "MANUAL-D"), (part_e, "OTHER-E"),
        (part_f, "MANUAL-BRP-F"), (part_g, "MANUAL-PROX-G"),
    ):
        _sell(env, part, number)


def _numbers(rows):
    return {row["number"] for row in rows}


def _sheet_numbers(content):
    sheet = openpyxl.load_workbook(BytesIO(content))[SHEET]
    numbers = []
    row = DATA_ROW
    while sheet[f"B{row}"].value not in (None, ""):
        numbers.append(str(sheet[f"B{row}"].value))
        row += 1
    return sheet, numbers


# --- A. Классификатор -----------------------------------------------------------


@pytest.mark.parametrize("manufacturer", ["BRP", "PROX", " brp ", "prox", "Brp", "PRO-X"])
def test_imported_brp_and_prox_are_original(manufacturer):
    assert customs_group(manufacturer, imported=True) == CUSTOMS_ORIGINAL


@pytest.mark.parametrize("manufacturer", ["BRP", "PROX", " brp ", "PRO-X", "", None, "BRONCO"])
def test_a_manual_part_is_analog_whatever_its_manufacturer(manufacturer):
    assert customs_group(manufacturer, imported=False) == CUSTOMS_ANALOG


@pytest.mark.parametrize(
    "manufacturer",
    ["BRONCO", "SPI", "POLARIS", "MOTUL", "OTHER", "", "   ", None, "BRP2", "XPROX"],
)
def test_everything_else_is_analog(manufacturer):
    assert customs_group(manufacturer, imported=True) == CUSTOMS_ANALOG


def test_group_values_match_customs_order_types():
    assert CUSTOMS_ORIGINAL == CustomsOrder.OrderType.ORIGINAL
    assert CUSTOMS_ANALOG == CustomsOrder.OrderType.ANALOG


def test_unknown_group_is_rejected():
    with pytest.raises(ValueError):
        customs_group_rows("everything")


# --- A2. Происхождение «вручную / импорт» -------------------------------------------


def test_provenance_is_the_catalog_link_not_the_category_or_manufacturer(env):
    brp_part = _imported_brp(env, "BRP-LINK")
    imported = _import_aftermarket([("PROX", "PROX-LINK")])
    prox_part = imported["PROX-LINK"]
    polaris = promote_polaris(
        PolarisCatalogPart.objects.create(part_number="POL-LINK", part_name="SEAL"),
        by=env["admin"],
    )
    manual = create_manual_part(
        name="РУЧНАЯ", article="MANUAL-LINK", price="1000", manufacturer_name="BRP",
    )
    ids = [brp_part.pk, prox_part.pk, polaris.pk, manual.pk]
    assert imported_part_ids(ids) == {brp_part.pk, prox_part.pk, polaris.pk}

    # Категорию можно сменить в карточке, поэтому она происхождение не решает.
    manual.category = Category.objects.create(name="Вариатор")
    manual.save(update_fields=["category"])
    brp_part.category, _ = Category.objects.get_or_create(name=MANUAL_CATEGORY_NAME)
    brp_part.save(update_fields=["category"])
    assert imported_part_ids(ids) == {brp_part.pk, prox_part.pk, polaris.pk}
    assert imported_part_ids([]) == set()


@pytest.mark.parametrize("brand", ["SPI", "MOTUL", "OTHER"])
def test_an_imported_non_brp_brand_is_analog(env, brand):
    part = _import_aftermarket([(brand, f"{brand}-IMP")])[f"{brand}-IMP"]
    _receive(env, part)
    _card(part, manufacturer=brand)
    _sell(env, part, f"{brand}-IMP")
    assert _numbers(historical_analog_customs_rows()) == {f"{brand}-IMP"}
    assert historical_customs_rows() == []
    assert eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL) == []


def test_an_imported_polaris_part_is_analog(env):
    part = promote_polaris(
        PolarisCatalogPart.objects.create(
            part_number="3610075", part_name="SEAL", wholesale_price_usd=Decimal("6"),
        ),
        by=env["admin"],
    )
    _receive(env, part)
    _card(part, manufacturer="POLARIS")
    _sell(env, part, "3610075")
    assert _numbers(historical_analog_customs_rows()) == {"3610075"}
    assert historical_customs_rows() == []


def test_a_manual_part_sold_under_a_brp_catalog_number_stays_analog(env):
    """Номер совпал с позицией BRP-прайса, производитель в строке - BRP, но
    деталь заведена вручную: в оригиналы она не попадает."""
    BrpCatalogPart.objects.create(
        material_no="219800345", part_desc="BELT DRIVE", wholesale_price_usd=Decimal("28"),
    )
    part = create_manual_part(name="РЕМЕНЬ", article="219800345", price="1000")
    _receive(env, part)
    _card(part, manufacturer="BRP")
    _sell(env, part, "219800345")
    (row,) = customs_export_rows()
    assert row["manufacturer"] == "BRP"
    assert row["customs_group"] == CUSTOMS_ANALOG
    assert eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL) == []
    assert _numbers(eligible_customs_sources(CustomsOrder.OrderType.ANALOG)) == {"219800345"}


def test_manual_part_given_brp_later_never_moves_to_originals(client, env):
    """Ручная деталь продана как аналог, затем ей выбрали BRP: строка остаётся
    в аналогах и в выгрузке, и в «Отправить в заказ»."""
    part = create_manual_part(name="РУЧНАЯ", article="LATER-BRP", price="1000")
    _receive(env, part)
    card = _card(part, manufacturer="")
    _sell(env, part, "LATER-BRP")
    assert _numbers(historical_analog_customs_rows()) == {"LATER-BRP"}

    part.manufacturer, _ = Manufacturer.objects.get_or_create(name="PROX")
    part.save(update_fields=["manufacturer"])
    card.manufacturer = "PROX"
    card.save()
    _sell(env, part, "LATER-BRP")

    assert historical_customs_rows() == []
    assert _numbers(historical_analog_customs_rows()) == {"LATER-BRP"}
    assert eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL) == []
    client.force_login(env["admin"])
    response = client.get(reverse("actions_export"), follow=True)
    assert "Нет оригиналов (BRP, PROX)" in response.content.decode()


# --- B-E. Паритет выгрузок и очередей заказа на смешанном заказе ----------------------


def test_excel_exports_split_by_manufacturer(env):
    _mixed_order(env)
    assert _numbers(historical_customs_rows()) == ORIGINALS
    assert _numbers(historical_analog_customs_rows()) == ANALOGS


def test_send_to_order_queues_use_the_same_sets_as_the_exports(env):
    _mixed_order(env)
    original_queue = eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL)
    analog_queue = eligible_customs_sources(CustomsOrder.OrderType.ANALOG)
    assert _numbers(original_queue) == _numbers(historical_customs_rows()) == ORIGINALS
    assert _numbers(analog_queue) == _numbers(historical_analog_customs_rows()) == ANALOGS
    assert all(not row["is_analog"] for row in original_queue)
    assert all(row["is_analog"] for row in analog_queue)


def test_every_line_is_original_xor_analog(env):
    _mixed_order(env)
    rows = customs_export_rows()
    originals = _numbers(historical_customs_rows())
    analogs = _numbers(historical_analog_customs_rows())
    assert not originals & analogs
    assert originals | analogs == _numbers(rows)
    assert len(historical_customs_rows()) + len(historical_analog_customs_rows()) == len(rows)

    sources = customs_sources(unassigned_only=True)
    original_queue = _numbers(eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL))
    analog_queue = _numbers(eligible_customs_sources(CustomsOrder.OrderType.ANALOG))
    assert not original_queue & analog_queue
    assert original_queue | analog_queue == _numbers(sources)


def test_downloaded_workbooks_match_the_groups(client, env):
    _mixed_order(env)
    client.force_login(env["admin"])
    _, original = _sheet_numbers(client.get(reverse("actions_export")).content)
    _, analog = _sheet_numbers(client.get(reverse("actions_analog_export")).content)
    assert set(original) == ORIGINALS and len(original) == 2
    assert set(analog) == ANALOGS and len(analog) == len(ANALOGS)


def test_analog_order_freezes_only_analog_lines(env):
    _mixed_order(env)
    sources = eligible_customs_sources(CustomsOrder.OrderType.ANALOG)
    last = sources[-1]
    order = create_customs_order_from_boundary(
        number=7, boundary_source=(last["source"], last["source_id"]),
        selection_token=selection_payload(sources, order_type=CustomsOrder.OrderType.ANALOG),
        order_type=CustomsOrder.OrderType.ANALOG, by=env["admin"],
    )
    assert set(order.lines.values_list("article", flat=True)) == ANALOGS
    assert all(order.lines.values_list("is_analog", flat=True))
    # Включённые аналоги ушли из очереди и выгрузки, оригиналы остались.
    assert eligible_customs_sources(CustomsOrder.OrderType.ANALOG) == []
    assert _numbers(eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL)) == ORIGINALS
    assert historical_analog_customs_rows(unassigned_only=True) == []
    assert _numbers(historical_customs_rows(unassigned_only=True)) == ORIGINALS


def test_original_order_freezes_only_original_lines(env):
    _mixed_order(env)
    sources = eligible_customs_sources(CustomsOrder.OrderType.ORIGINAL)
    last = sources[-1]
    order = create_customs_order_from_boundary(
        number=8, boundary_source=(last["source"], last["source_id"]),
        selection_token=selection_payload(sources, order_type=CustomsOrder.OrderType.ORIGINAL),
        order_type=CustomsOrder.OrderType.ORIGINAL, by=env["admin"],
    )
    assert set(order.lines.values_list("article", flat=True)) == ORIGINALS
    assert not any(order.lines.values_list("is_analog", flat=True))
    assert _numbers(eligible_customs_sources(CustomsOrder.OrderType.ANALOG)) == ANALOGS


# --- F. Веса и колонки сохраняются ------------------------------------------------


def test_customs_columns_and_weights_are_preserved_in_both_exports(client, env):
    _mixed_order(env)
    client.force_login(env["admin"])
    original, numbers = _sheet_numbers(client.get(reverse("actions_export")).content)
    row = DATA_ROW + numbers.index("BRP-A")
    assert original[f"E{row}"].value == "BRP"
    assert original[f"F{row}"].value == "CANADA"
    assert Decimal(str(original[f"G{row}"].value)) == Decimal("1.100")
    assert Decimal(str(original[f"H{row}"].value)) == Decimal("1.000")
    assert original[f"I{row}"].value == f"=J{row}*G{row}"
    assert original[f"L{row}"].value == f"=K{row}*J{row}"

    analog, numbers = _sheet_numbers(client.get(reverse("actions_analog_export")).content)
    row = DATA_ROW + numbers.index("BRONCO-C")
    assert analog[f"E{row}"].value == "BRONCO"
    assert Decimal(str(analog[f"G{row}"].value)) == Decimal("0.750")
    assert Decimal(str(analog[f"H{row}"].value)) == Decimal("0.700")
    assert analog[f"I{row}"].value == f"=J{row}*G{row}"


# --- G-H. Пустые группы -------------------------------------------------------------


def test_empty_original_export_explains_instead_of_an_empty_file(client, env):
    part = create_manual_part(name="ТОЛЬКО АНАЛОГ", article="ONLY-ALT", price="1000")
    _receive(env, part)
    _card(part, manufacturer="")
    _sell(env, part, "ONLY-ALT")
    client.force_login(env["admin"])
    response = client.get(reverse("actions_export"), follow=True)
    assert response.redirect_chain[-1][0] == reverse("actions_report")
    assert "Нет оригиналов (BRP, PROX), ещё не включённых в таможенный заказ." in (
        response.content.decode()
    )


def test_empty_analog_export_explains_instead_of_an_empty_file(client, env):
    client.force_login(env["admin"])
    response = client.get(reverse("actions_analog_export"), follow=True)
    assert response.redirect_chain[-1][0] == reverse("actions_report")
    assert "Нет аналогов, ещё не включённых в таможенный заказ." in response.content.decode()


def test_empty_selection_pages_explain_each_group(client, env):
    client.force_login(env["admin"])
    url = reverse("customs_order_selection")
    original = client.get(url + "?order_type=original").content.decode()
    analog = client.get(url + "?order_type=analog").content.decode()
    assert "Отправить в заказ оригинал" in original
    assert "Свободных оригиналов (BRP, PROX) нет" in original
    assert "Отправить в заказ аналоги" in analog
    assert "Свободных аналогов нет" in analog


# --- I-J. Кнопки отчёта ----------------------------------------------------------------


def test_report_shows_the_two_by_two_customs_buttons(client, env):
    client.force_login(env["admin"])
    html = client.get(reverse("actions_report")).content.decode()
    assert "Сканер действий" not in html
    assert "Экспорт в Excel для таможни" not in html
    assert "Таможенный экспорт аналогов" not in html

    original_at = html.index('data-customs-group="original"')
    analog_at = html.index('data-customs-group="analog"')
    assert original_at < analog_at
    original_block = html[original_at:analog_at]
    analog_block = html[analog_at:html.index("</div>", analog_at)]

    # В каждой колонке Excel сверху, отправка в заказ прямо под ним.
    assert original_block.index("Экспорт в Excel оригинал") < original_block.index(
        "Отправить в заказ оригинал"
    )
    assert analog_block.index("Экспорт в Excel аналоги") < analog_block.index(
        "Отправить в заказ аналоги"
    )
    assert reverse("actions_export") in original_block
    assert "order_type=original" in original_block
    assert reverse("actions_analog_export") in analog_block
    assert "order_type=analog" in analog_block
    # Обе Excel-кнопки синие (primary).
    for block, label in (
        (original_block, "Экспорт в Excel оригинал"),
        (analog_block, "Экспорт в Excel аналоги"),
    ):
        tag = block[block.rindex("<a", 0, block.index(label)):block.index(label)]
        assert "btn--primary" in tag
    assert "—" not in html and "–" not in html


def test_customs_orders_list_offers_both_send_actions(client, env):
    client.force_login(env["admin"])
    html = client.get(reverse("customs_orders_list")).content.decode()
    selection = reverse("customs_order_selection")
    assert f"{selection}?order_type=original" in html
    assert f"{selection}?order_type=analog" in html
    assert "Отправить в заказ оригинал" in html
    assert "Отправить в заказ аналоги" in html


# --- Аудит: группа таможни по происхождению (read-only) -----------------------------


def test_classification_audit_reports_provenance_groups_without_writing(env):
    import json
    from io import StringIO

    from django.core.management import call_command

    from apps.catalog.models import PartType, Unit

    imported_brp = _imported_brp(env, "AUD-BRP")
    _card(imported_brp, manufacturer="BRP")
    imported_prox = _import_aftermarket([("PROX", "AUD-PROX")])["AUD-PROX"]
    _card(imported_prox, manufacturer="PROX")
    manual_brp = create_manual_part(
        name="РУЧНАЯ BRP", article="AUD-M-BRP", price="1000", manufacturer_name="BRP",
    )
    _card(manual_brp, manufacturer="BRP")
    manual_prox = create_manual_part(
        name="РУЧНАЯ PROX", article="AUD-M-PROX", price="1000", manufacturer_name="PROX",
    )
    _card(manual_prox, manufacturer="PROX")
    legacy = PartType.objects.create(
        name="СТАРАЯ BRP", category=Category.objects.create(name="Вариатор"),
        manufacturer=Manufacturer.objects.get_or_create(name="BRP")[0],
        unit=Unit.objects.get(name="Штука"), tracking_mode=PartType.TrackingMode.BULK,
    )
    _card(legacy, manufacturer="BRP")
    before = list(PartCustomsInfo.objects.order_by("pk").values_list("pk", "manufacturer"))

    out = StringIO()
    call_command("audit_customs_manufacturer_classification", "--json", "--list", "0", stdout=out)
    payload = json.loads(out.getvalue())

    assert payload["imported_brp_original"] == 1
    assert payload["imported_prox_original"] == 1
    assert payload["brp_labelled_without_import"] == 2  # ручная BRP + старая BRP
    assert payload["prox_labelled_without_import"] == 1
    assert payload["brp_prox_manual_to_analog"] == 2
    assert payload["brp_prox_legacy_unproven_needs_owner_review"] == 1
    assert payload["imported_brp_prox_label_not_original"] == 0
    assert list(
        PartCustomsInfo.objects.order_by("pk").values_list("pk", "manufacturer")
    ) == before

    listing = StringIO()
    call_command("audit_customs_manufacturer_classification", "--list", "10", stdout=listing)
    text = listing.getvalue()
    assert f"PartType #{legacy.pk} " in text
    assert f"PartType #{manual_brp.pk} " in text
