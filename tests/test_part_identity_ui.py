"""Унификация идентификации детали в UI (ветка unify-part-identity-ui).

Пользовательский стандарт: в любой операционной таблице деталь опознаётся
названием + exact-артикулом (BRP material_no -> Polaris part_number ->
primary-номер допустимого вида -> OEM/артикул из EXACT_NUMBER_KINDS ->
«Артикул не указан»). Внутренний справочный (internal_ref) артикулом
не становится никогда.
Аналог/replacement/superseded/источник цены identity не являются никогда;
`.numbers.first()` в identity-хелперах запрещён (ordering ставит analog
раньше oem). Лот/экземпляр/номер документа — вторичная информация.

Фикстуры скопированы из test_customs_export.py (принятый в проекте паттерн:
cross-file import фикстур ловит ruff F811).
"""
from decimal import Decimal
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.presentation import manufacturer_display, part_exact_number
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris
from apps.procurement.forms import BatchLineForm
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.receipts.forms import ReceiptLineForm
from apps.receipts.services import add_line, create_receipt, post_receipt
from apps.repairs.forms import AddRepairLotForm
from apps.repairs.services import (
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.forms import AddLotForm, AddSaleLotForm
from apps.sales.models import Sale
from apps.sales.services import (
    add_stock_lot_to_reservation,
    add_stock_lot_to_sale,
    complete_sale,
    create_reservation,
    create_sale,
)
from apps.stocktaking.services import add_stock_lot_count_line, create_inventory_count
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.writeoffs.forms import AddWriteOffLotForm
from apps.writeoffs.services import (
    add_stock_lot_to_write_off,
    complete_write_off,
    create_write_off,
)

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


def _stock(part, location, qty, sup, admin):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(str(qty)),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def env(db, admin):
    sup = Supplier.objects.create(name="Стартовый ввод")
    loc = StorageLocation.objects.create(
        name="C04", code="S04-L03-D01-C04", storage_allowed=True, is_active=True
    )
    return {"sup": sup, "loc": loc, "admin": admin}


def _brp(env, *, material, desc="BELT DRIVE", qty=5, replacement=""):
    brp = BrpCatalogPart.objects.create(
        material_no=material, part_desc=desc, replacement_no_1=replacement,
        retail_price_usd=Decimal("10"), wholesale_price_usd=Decimal("7"),
    )
    part = promote_brp(brp, by=env["admin"])
    lot = _stock(part, env["loc"], qty, env["sup"], env["admin"])
    return part, lot


def _polaris(env, *, number, desc="SEAL", qty=5, superseded=""):
    pol = PolarisCatalogPart.objects.create(
        part_number=number, part_name=desc, superseded_number=superseded,
        wholesale_price_usd=Decimal("6"), retail_price_usd=Decimal("20"),
    )
    part = promote_polaris(pol, by=env["admin"])
    lot = _stock(part, env["loc"], qty, env["sup"], env["admin"])
    return part, lot


def _plain_part(env, *, name, numbers=(), qty=0):
    """Складская карточка без BRP/Polaris: numbers = [(value, kind, primary)]."""
    part = PartType.objects.create(
        name=name, category=Category.objects.create(name=f"cat-{name}"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    for value, kind, primary in numbers:
        PartNumber.objects.create(part=part, value=value, kind=kind, is_primary=primary)
    lot = _stock(part, env["loc"], qty, env["sup"], env["admin"]) if qty else None
    return part, lot


def _sold(env, positions):
    """Проведённая продажа: positions = [(lot, qty)]."""
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    for lot, qty in positions:
        add_stock_lot_to_sale(
            sale, lot, Decimal(str(qty)), unit_price=Decimal("100"), by=env["admin"]
        )
    return complete_sale(sale, by=env["admin"])


def _login(client, make_user, *, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


KIND = PartNumber.Kind


# --- 1-9. Canonical exact identity -----------------------------------------------------


def test_brp_material_no_is_identity(env):
    part, _ = _brp(env, material="219800345")
    assert part_exact_number(part) == "219800345"


def test_polaris_part_number_is_identity(env):
    part, _ = _polaris(env, number="3610075")
    assert part_exact_number(part) == "3610075"


def test_primary_warehouse_number_is_identity(env):
    part, _ = _plain_part(env, name="ПРИМАРИ", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
        ("PRIM-222", KIND.ARTICLE, True),
        ("OEM-333", KIND.OEM, False),
    ])
    assert part_exact_number(part) == "PRIM-222"


def test_oem_number_used_without_primary(env):
    part, _ = _plain_part(env, name="ОЕМКА", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
        ("OEM-333", KIND.OEM, False),
    ])
    assert part_exact_number(part) == "OEM-333"


def test_analog_never_identity(env):
    part, _ = _plain_part(env, name="АНАЛОГОВАЯ", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
    ])
    # Ловушка сортировки существует: первый номер по умолчанию — аналог...
    assert part.numbers.first().value == "099-ANALOG-1"
    # ...но identity им не становится.
    assert part_exact_number(part) == "Артикул не указан"
    assert part_exact_number(part, default="") == ""


def test_internal_only_never_identity(env):
    part, _ = _plain_part(env, name="ВНУТРЕННЯЯ", numbers=[
        ("INT-001", KIND.INTERNAL_REF, False),
    ])
    assert part_exact_number(part) == "Артикул не указан"
    assert part_exact_number(part, default="") == ""


def test_internal_plus_analog_never_identity(env):
    part, _ = _plain_part(env, name="ВНУТР-АНАЛОГ", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
        ("INT-001", KIND.INTERNAL_REF, False),
    ])
    assert part_exact_number(part) == "Артикул не указан"


def test_oem_wins_over_internal(env):
    # internal создан ПЕРВЫМ (меньший pk) — OEM всё равно выигрывает.
    part, _ = _plain_part(env, name="ОЕМ-ВНУТР", numbers=[
        ("INT-001", KIND.INTERNAL_REF, False),
        ("OEM-333", KIND.OEM, False),
    ])
    assert part_exact_number(part) == "OEM-333"


def test_primary_internal_never_identity(env):
    # Даже явный primary не делает внутренний справочный номер артикулом.
    part, _ = _plain_part(env, name="ПРИМ-ВНУТР", numbers=[
        ("INT-001", KIND.INTERNAL_REF, True),
        ("OEM-333", KIND.OEM, False),
    ])
    assert part_exact_number(part) == "OEM-333"
    solo, _ = _plain_part(env, name="ПРИМ-ВНУТР-СОЛО", numbers=[
        ("INT-001", KIND.INTERNAL_REF, True),
    ])
    assert part_exact_number(solo) == "Артикул не указан"


def test_replacement_never_identity(env):
    part, _ = _brp(env, material="420931285", replacement="420931284")
    assert part_exact_number(part) == "420931285"


def test_superseded_never_identity(env):
    part, _ = _polaris(env, number="2222222", superseded="1111111")
    assert part_exact_number(part) == "2222222"


def test_brp_and_polaris_same_number_not_mixed(env):
    brp_part, _ = _brp(env, material="3610075", desc="BRP PART")
    pol_part, _ = _polaris(env, number="3610075", desc="POLARIS PART")
    assert part_exact_number(brp_part) == part_exact_number(pol_part) == "3610075"
    assert manufacturer_display(brp_part) == "BRP"
    assert manufacturer_display(pol_part) == "POLARIS"


def test_numbers_first_not_used_in_identity_helpers():
    """Регрессия: вызов `<...>.numbers.first()` запрещён в identity-хелперах.

    Проверяется по AST (не по тексту): docstring, объясняющий запрет,
    срабатывать не должен.
    """
    import ast

    root = Path(__file__).resolve().parents[1]
    for rel in ("apps/inventory/presentation.py", "apps/labels/views.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr != "first":
                continue
            target = node.func.value
            assert not (
                isinstance(target, ast.Attribute) and target.attr == "numbers"
            ), f"{rel}: найден вызов .numbers.first()"


# --- 10-12. Поиск -----------------------------------------------------------------------


@pytest.fixture
def analog_part(env):
    part, lot = _plain_part(env, name="IDENTITY SEAL", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
        ("555000777", KIND.OEM, True),
    ], qty=3)
    return part


def test_search_shows_exact_number_first(client, make_user, env, analog_part):
    _login(client, make_user)
    html = client.get(reverse("part_search") + "?q=IDENTITY SEAL").content.decode()
    assert '<span class="code-pill">555000777</span>' in html


def test_search_labels_analogs(client, make_user, env, analog_part):
    _login(client, make_user)
    html = client.get(reverse("part_search") + "?q=IDENTITY SEAL").content.decode()
    assert "Аналоги:" in html
    assert "099-ANALOG-1" in html


def test_search_analog_not_presented_as_identity(client, make_user, env, analog_part):
    _login(client, make_user)
    html = client.get(reverse("part_search") + "?q=IDENTITY SEAL").content.decode()
    assert '<span class="code-pill">099-ANALOG-1</span>' not in html
    # exact идёт раньше упоминания аналога
    assert html.index("555000777") < html.index("099-ANALOG-1")


# --- 13-15. Этикетки ---------------------------------------------------------------------


def test_label_prints_exact_number(client, make_user, env):
    part, _ = _plain_part(env, name="ЭТИКЕТКА", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
        ("PRIM-222", KIND.ARTICLE, True),
    ])
    _login(client, make_user)
    html = client.get(reverse("label_part", args=[part.pk])).content.decode()
    assert "PRIM-222" in html


def test_label_prints_brp_and_polaris_numbers(client, make_user, env):
    brp_part, _ = _brp(env, material="219800345")
    pol_part, _ = _polaris(env, number="3610075")
    _login(client, make_user)
    assert "219800345" in client.get(reverse("label_part", args=[brp_part.pk])).content.decode()
    assert "3610075" in client.get(reverse("label_part", args=[pol_part.pk])).content.decode()


def test_label_never_falls_back_to_analog(client, make_user, env):
    part, _ = _plain_part(env, name="ТОЛЬКО АНАЛОГ", numbers=[
        ("099-ANALOG-1", KIND.ANALOG, False),
    ])
    _login(client, make_user)
    html = client.get(reverse("label_part", args=[part.pk])).content.decode()
    assert "099-ANALOG-1" not in html  # ложный номер не печатается


# --- 16-22. Формы выбора лота/детали -------------------------------------------------------


def _lot_labels(form):
    return [label for value, label in form.fields["lot"].choices if value]


def test_sale_lot_option_has_name_and_number(env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    labels = _lot_labels(AddSaleLotForm())
    assert any("BELT DRIVE" in x and "219800345" in x for x in labels)


def test_repair_lot_option_has_name_and_number(env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    labels = _lot_labels(AddRepairLotForm())
    assert any("BELT DRIVE" in x and "219800345" in x for x in labels)


def test_write_off_lot_option_has_name_and_number(env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    labels = _lot_labels(AddWriteOffLotForm())
    assert any("BELT DRIVE" in x and "219800345" in x for x in labels)


def test_reservation_lot_option_has_name_and_number(env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    labels = _lot_labels(AddLotForm())
    assert any("BELT DRIVE" in x and "219800345" in x for x in labels)


def test_batch_part_option_has_name_and_number(env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    labels = [label for value, label in BatchLineForm().fields["part_type"].choices if value]
    assert any("BELT DRIVE" in x and "219800345" in x for x in labels)


def test_receipt_part_option_has_name_and_number(env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    labels = [label for value, label in ReceiptLineForm().fields["part_type"].choices if value]
    assert any("BELT DRIVE" in x and "219800345" in x for x in labels)


def test_manufacturer_disambiguates_same_number_in_options(env):
    _brp(env, material="3610075", desc="BRP PART")
    _polaris(env, number="3610075", desc="POLARIS PART")
    labels = _lot_labels(AddSaleLotForm())
    assert any("3610075" in x and "BRP" in x for x in labels)
    assert any("3610075" in x and "POLARIS" in x for x in labels)


# --- 23-28. Продажи -------------------------------------------------------------------------


def test_sale_list_single_position_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    _sold(env, [(lot, 1)])
    _login(client, make_user)
    html = client.get(reverse("sale_list")).content.decode()
    assert "BELT DRIVE" in html
    assert '<span class="code-pill">219800345</span>' in html
    assert "× 1" in html
    assert "ещё" not in html


def test_sale_list_multiple_positions_show_first_and_more(client, make_user, env):
    _, lot1 = _brp(env, material="111000001", desc="AAA PART")
    _, lot2 = _brp(env, material="222000002", desc="BBB PART")
    _, lot3 = _brp(env, material="333000003", desc="CCC PART")
    _sold(env, [(lot1, 1), (lot2, 2), (lot3, 1)])
    _login(client, make_user)
    html = client.get(reverse("sale_list")).content.decode()
    assert "AAA PART" in html  # первая позиция
    assert "ещё 2 позиции · всего 3" in html
    assert "CCC PART" not in html  # полный список не разворачивается


def test_sale_detail_shows_name_and_number_separately(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    sale = _sold(env, [(lot, 2)])
    _login(client, make_user)
    html = client.get(reverse("sale_detail", args=[sale.pk])).content.decode()
    assert "<th>Название детали</th>" in html
    assert "<th>Артикул</th>" in html
    assert '<span class="code-pill">219800345</span>' in html
    assert "<th>Что</th>" not in html


def test_sale_detail_lot_is_secondary_not_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    sale = _sold(env, [(lot, 1)])
    _login(client, make_user)
    html = client.get(reverse("sale_detail", args=[sale.pk])).content.decode()
    assert "Источник остатка" in html
    assert f"лот #{lot.pk}" in html  # вторично, в колонке источника
    assert '<span class="code-pill">219800345</span>' in html  # артикул основной


def test_cancelled_sale_renders_composition(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    sale = _sold(env, [(lot, 1)])
    sale.status = Sale.Status.VOIDED
    sale.save(update_fields=["status"])
    _login(client, make_user)
    html = client.get(reverse("sale_list")).content.decode()
    assert "BELT DRIVE" in html
    assert client.get(reverse("sale_detail", args=[sale.pk])).status_code == 200


def test_sale_list_no_n_plus_one(client, make_user, env):
    part, lot = _brp(env, material="111000001", desc="AAA PART", qty=50)
    _sold(env, [(lot, 1)])
    _login(client, make_user)
    with CaptureQueriesContext(connection) as first:
        client.get(reverse("sale_list"))
    for _i in range(8):
        _sold(env, [(lot, 1)])
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("sale_list"))
    # 8 дополнительных документов не должны давать линейного роста запросов.
    assert len(many) <= len(first) + 3, (len(first), len(many))


# --- 29-32. Резервы, ремонты, возвраты, списания -------------------------------------------


def _assert_identity_table(html):
    assert "<th>Название детали</th>" in html
    assert "<th>Артикул</th>" in html
    assert '<span class="code-pill">219800345</span>' in html


def test_reservation_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    reservation = create_reservation(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_reservation(reservation, lot, Decimal("1"), by=env["admin"])
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("reservation_detail", args=[reservation.pk])).content.decode()
    )


def test_repair_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    order = create_repair_order(customer_name="Клиент", by=env["admin"])
    add_stock_lot_to_repair_order(order, lot, Decimal("1"), by=env["admin"])
    order = complete_repair_order(order, by=env["admin"])
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("repair_order_detail", args=[order.pk])).content.decode()
    )


def test_return_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    sale = _sold(env, [(lot, 2)])
    ret = create_return(source=sale, reason="брак", by=env["admin"])
    line = sale.lines.first()
    add_sale_line_return(
        ret, line, Decimal("1"), to_location=env["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE, by=env["admin"],
    )
    ret = complete_return(ret, by=env["admin"])
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    )


def test_write_off_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    doc = create_write_off(reason="defect", by=env["admin"])
    add_stock_lot_to_write_off(doc, lot, Decimal("1"), by=env["admin"])
    doc = complete_write_off(doc, by=env["admin"])
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("write_off_detail", args=[doc.pk])).content.decode()
    )


# --- 33-38. Склад ----------------------------------------------------------------------------


def test_lot_list_shows_identity(client, make_user, env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    _login(client, make_user)
    html = client.get(reverse("lot_list")).content.decode()
    _assert_identity_table(html)
    assert "<th>Лот</th>" in html  # технический номер лота остался вторичной ссылкой


def test_item_list_separates_exact_and_internal_numbers(client, make_user, env):
    from apps.inventory.services import create_part_items

    brp = BrpCatalogPart.objects.create(
        material_no="219800345", part_desc="SERIAL PART",
        retail_price_usd=Decimal("10"), wholesale_price_usd=Decimal("7"),
    )
    part = promote_brp(brp, by=env["admin"])
    part.tracking_mode = PartType.TrackingMode.SERIAL
    part.save(update_fields=["tracking_mode"])
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("1"),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    item = create_part_items(line, 1)[0]
    _login(client, make_user)
    html = client.get(reverse("item_list")).content.decode()
    assert "<th>Внутренний номер</th>" in html
    assert '<span class="code-pill">219800345</span>' in html
    assert item.internal_number in html


def test_balance_list_shows_identity(client, make_user, env):
    _brp(env, material="219800345", desc="BELT DRIVE")
    _login(client, make_user)
    html = client.get(reverse("balance_list")).content.decode()
    _assert_identity_table(html)
    assert "Кэш" not in html  # разработческая подпись заменена


def test_receipt_shows_identity(client, make_user, env):
    part, _ = _brp(env, material="219800345", desc="BELT DRIVE")
    receipt = create_receipt(supplier=env["sup"], by=env["admin"])
    add_line(
        receipt, part_type=part, quantity=Decimal("2"),
        unit_cost_rub=Decimal("100"), location=env["loc"],
    )
    receipt = post_receipt(receipt, by=env["admin"])
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("receipt_detail", args=[receipt.pk])).content.decode()
    )


def test_batch_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("batch_detail", args=[lot.batch_id])).content.decode()
    )


def test_stocktaking_shows_identity(client, make_user, env):
    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    doc = create_inventory_count(scope_location=env["loc"], by=env["admin"])
    add_stock_lot_count_line(doc, lot, by=env["admin"])
    _login(client, make_user)
    _assert_identity_table(
        client.get(reverse("inventory_count_detail", args=[doc.pk])).content.decode()
    )


# --- 39-41. Отчёты и экспорты -----------------------------------------------------------------


@pytest.fixture
def low_stock_part(env):
    part, _ = _brp(env, material="219800345", desc="BELT DRIVE", qty=2)
    part.min_stock_level = Decimal("10")
    part.save(update_fields=["min_stock_level"])
    return part


def test_low_stock_shows_identity(client, make_user, env, low_stock_part):
    _login(client, make_user)
    html = client.get(reverse("reports_stock")).content.decode()
    assert '<span class="code-pill">219800345</span>' in html
    dashboard = client.get(reverse("dashboard")).content.decode()
    assert '<span class="code-pill">219800345</span>' in dashboard


def test_low_stock_csv_has_identity_columns(client, make_user, env, low_stock_part):
    _login(client, make_user)
    body = client.get(reverse("reports_export_low_stock")).content.decode("utf-8-sig")
    header = body.splitlines()[0]
    assert header == "Название детали;Артикул;Производитель;Доступно;Минимум"
    assert "219800345" in body
    assert "BRP" in body


def test_customs_export_unchanged(client, make_user, env):
    from io import BytesIO

    import openpyxl

    from apps.actions.services import perform_action

    part, lot = _brp(env, material="219800345", desc="BELT DRIVE")
    perform_action(
        part=part, location=env["loc"], action_type="sale", quantity="1",
        customer_comment="Иванов", scanned_number="219800345", by=env["admin"],
    )
    _login(client, make_user)
    resp = client.get(reverse("actions_export"))
    assert resp.status_code == 200
    sheet = openpyxl.load_workbook(BytesIO(resp.content))["Лист1"]
    assert str(sheet["B10"].value) == "219800345"  # exact identity как и раньше


# --- 42. Success-сообщение сканера ------------------------------------------------------------


def test_scanner_success_message_contains_identity(client, make_user, env):
    part, _ = _brp(env, material="219800345", desc="BELT DRIVE")
    _login(client, make_user)
    resp = client.post(
        reverse("actions_perform"),
        {
            "part_id": part.pk, "location_id": env["loc"].pk,
            "action_type": "sale", "quantity": "1",
            "customer_comment": "Иванов", "q": "219800345",
        },
        follow=True,
    )
    text = resp.content.decode()
    assert "BELT DRIVE" in text
    assert "артикул 219800345" in text


# --- Query-count: списки без линейного N+1 -----------------------------------------------------


def test_lot_list_no_n_plus_one(client, make_user, env):
    _brp(env, material="111000001", desc="AAA PART")
    _login(client, make_user)
    with CaptureQueriesContext(connection) as first:
        client.get(reverse("lot_list"))
    for i in range(8):
        _brp(env, material=f"22200000{i}", desc=f"PART {i}")
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("lot_list"))
    assert len(many) <= len(first) + 3, (len(first), len(many))


def test_search_no_n_plus_one(client, make_user, env):
    _plain_part(env, name="IDENT-PART 0", numbers=[("N-0", KIND.OEM, True)], qty=1)
    _login(client, make_user)
    with CaptureQueriesContext(connection) as first:
        client.get(reverse("part_search") + "?q=IDENT-PART")
    for i in range(1, 8):
        _plain_part(env, name=f"IDENT-PART {i}", numbers=[(f"N-{i}", KIND.OEM, True)], qty=1)
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("part_search") + "?q=IDENT-PART")
    assert len(many) <= len(first) + 3, (len(first), len(many))


def test_low_stock_report_no_n_plus_one(client, make_user, env):
    def low_part(material):
        part, _ = _brp(env, material=material, qty=1)
        part.min_stock_level = Decimal("10")
        part.save(update_fields=["min_stock_level"])

    low_part("111000001")
    _login(client, make_user)
    with CaptureQueriesContext(connection) as first:
        client.get(reverse("reports_stock"))
    for i in range(5):
        low_part(f"33300000{i}")
    with CaptureQueriesContext(connection) as many:
        client.get(reverse("reports_stock"))
    assert len(many) <= len(first) + 3, (len(first), len(many))


# --- Internal-номер не подменяет артикул нигде в UI -------------------------------------------


@pytest.fixture
def internal_only_part(env):
    """Деталь с ЕДИНСТВЕННЫМ внутренним справочным номером и остатком."""
    part, lot = _plain_part(env, name="INTERNAL ONLY PART", numbers=[
        ("INT-777", KIND.INTERNAL_REF, True),
    ], qty=2)
    return part, lot


def test_form_option_does_not_use_internal_as_number(env, internal_only_part):
    part, lot = internal_only_part
    labels = [x for x in _lot_labels(AddSaleLotForm()) if "INTERNAL ONLY PART" in x]
    assert labels, "лот должен быть в списке"
    assert "INT-777" not in labels[0]  # internal не выдан за артикул
    assert "Артикул не указан" in labels[0]


def test_search_does_not_use_internal_as_number(client, make_user, env, internal_only_part):
    _login(client, make_user)
    html = client.get(reverse("part_search") + "?q=INTERNAL ONLY").content.decode()
    assert '<span class="code-pill">INT-777</span>' not in html
    assert "не указан" in html


def test_label_does_not_print_internal_as_number(client, make_user, env, internal_only_part):
    part, _ = internal_only_part
    _login(client, make_user)
    html = client.get(reverse("label_part", args=[part.pk])).content.decode()
    assert "INT-777" not in html


def test_low_stock_csv_does_not_use_internal_as_number(client, make_user, env, internal_only_part):
    part, _ = internal_only_part
    part.min_stock_level = Decimal("10")
    part.save(update_fields=["min_stock_level"])
    _login(client, make_user)
    body = client.get(reverse("reports_export_low_stock")).content.decode("utf-8-sig")
    row = next(line for line in body.splitlines() if "INTERNAL ONLY PART" in line)
    assert "INT-777" not in row  # колонка «Артикул» остаётся пустой
    html = client.get(reverse("reports_stock")).content.decode()
    assert '<span class="code-pill">INT-777</span>' not in html
