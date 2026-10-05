"""Таможня: оригиналы (BRP, PROX) против аналогов (всё остальное).

Правило владельца: ОРИГИНАЛ - только производитель BRP или PROX; АНАЛОГ - всё
остальное (BRONCO, прочие бренды, ручные детали, пустой производитель).
Один классификатор (apps.actions.services.customs_group) делит и обе
Excel-выгрузки, и обе очереди «Отправить в заказ», поэтому наборы строк
совпадают, а каждая строка попадает ровно в одну группу.
"""
from decimal import Decimal
from io import BytesIO

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
    perform_action,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.services import create_manual_part
from apps.customs_orders.models import CustomsOrder
from apps.customs_orders.services import (
    create_customs_order_from_boundary,
    customs_sources,
    eligible_customs_sources,
    selection_payload,
)
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.warehouse.models import StorageLocation

pytestmark = pytest.mark.django_db

SHEET = "Лист1"
DATA_ROW = 10
ORIGINALS = {"BRP-A", "PROX-B"}
ANALOGS = {"BRONCO-C", "MANUAL-D", "OTHER-E"}


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


def _mixed_order(env):
    """BRP A + PROX B - оригиналы; BRONCO C, ручная D и OTHER E - аналоги."""
    brp = BrpCatalogPart.objects.create(
        material_no="BRP-A", part_desc="BELT DRIVE", wholesale_price_usd=Decimal("28.15"),
    )
    part_a = promote_to_warehouse(brp, by=env["admin"])
    _receive(env, part_a)
    _card(part_a, manufacturer="BRP", gross="1.100", net="1.000")

    part_b = create_manual_part(
        name="ДЕТАЛЬ PROX", article="PROX-B", price="1000", manufacturer_name="PROX",
    )
    _receive(env, part_b)
    _card(part_b, manufacturer="PROX")

    part_c = create_manual_part(
        name="ДЕТАЛЬ BRONCO", article="BRONCO-C", price="1000", manufacturer_name="BRONCO",
    )
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

    for part, number in (
        (part_a, "BRP-A"), (part_b, "PROX-B"), (part_c, "BRONCO-C"),
        (part_d, "MANUAL-D"), (part_e, "OTHER-E"),
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
def test_only_brp_and_prox_are_original(manufacturer):
    assert customs_group(manufacturer) == CUSTOMS_ORIGINAL


@pytest.mark.parametrize(
    "manufacturer", ["BRONCO", "SPI", "POLARIS", "OTHER", "", "   ", None, "BRP2", "XPROX"],
)
def test_everything_else_is_analog(manufacturer):
    assert customs_group(manufacturer) == CUSTOMS_ANALOG


def test_group_values_match_customs_order_types():
    assert CUSTOMS_ORIGINAL == CustomsOrder.OrderType.ORIGINAL
    assert CUSTOMS_ANALOG == CustomsOrder.OrderType.ANALOG


def test_unknown_group_is_rejected():
    with pytest.raises(ValueError):
        customs_group_rows("everything")


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
    assert set(analog) == ANALOGS and len(analog) == 3


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
