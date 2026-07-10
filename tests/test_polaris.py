"""Polaris catalog import, lookup, counting, actions and customs export."""
from decimal import Decimal

import openpyxl
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.actions.services import actions_report, export_customs_xlsx, perform_action
from apps.brp.models import BrpCatalogPart
from apps.catalog.models import PartNumber
from apps.counting.services import convert_to_receipt, record_scan, start_session
from apps.inventory.models import StockBalance
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.polaris.importer import import_catalog
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink
from apps.polaris.services import (
    effective_customer_price_rub,
    find_polaris_by_number,
    find_polaris_price_source,
    promote_to_warehouse,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
HEADERS = ["part_number", "part_name", "superseded_number", "ОПТОВАЯ", "РОЗНИЦА", "uom"]


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


def _login(client, make_user, *, role=None, superuser=False, name="u"):
    make_user(name, role=role, is_superuser=superuser)
    client.login(username=name, password=PASSWORD)


def _make_xlsx(tmp_path, rows, name="polaris.xlsx"):
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(HEADERS)
    for row in rows:
        sheet.append(row)
    path = tmp_path / name
    workbook.save(path)
    return path


def _stock(part, location, admin, *, qty="3"):
    supplier = Supplier.objects.create(name="Polaris supplier")
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(qty),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(qty))
    receive_stock_lot(lot, by=admin)
    return lot


def test_import_polaris_dry_run_creates_nothing(db, tmp_path):
    path = _make_xlsx(tmp_path, [["042", "OIL SEAL", "", 20, 30, "10-Pack"]])
    summary = import_catalog(path)
    assert summary.mode == "dry-run"
    assert summary.created == 1
    assert PolarisCatalogPart.objects.count() == 0


def test_import_polaris_commit_and_idempotent_update(db, tmp_path):
    path = _make_xlsx(tmp_path, [["042", "OIL SEAL", "041", 20, 30, "10-Pack"]])
    summary = import_catalog(path, commit=True)
    assert summary.created == 1
    assert summary.with_superseded == 1
    part = PolarisCatalogPart.objects.get(part_number="042")
    assert part.part_number == "042"
    assert part.part_name == "OIL SEAL"
    assert part.superseded_number == "041"
    assert part.wholesale_price_usd == Decimal("20")
    assert part.retail_price_usd == Decimal("30")
    assert part.uom == "10-Pack"

    changed = _make_xlsx(tmp_path, [["042", "OIL SEAL UPDATED", "041", 20, 35, "3-Pack"]])
    again = import_catalog(changed, commit=True)
    assert again.created == 0
    assert again.updated == 1
    assert PolarisCatalogPart.objects.count() == 1
    part.refresh_from_db()
    assert part.part_name == "OIL SEAL UPDATED"
    assert part.retail_price_usd == Decimal("35")


def test_polaris_exact_part_number_wins_before_superseded(db):
    exact = PolarisCatalogPart.objects.create(
        part_number="100", part_name="EXACT", retail_price_usd=Decimal("9")
    )
    PolarisCatalogPart.objects.create(
        part_number="200", part_name="SUPERSEDING",
        superseded_number="100", retail_price_usd=Decimal("50"),
    )
    assert find_polaris_by_number("100") == exact


def test_polaris_price_source_can_differ_from_identity(db):
    exact = PolarisCatalogPart.objects.create(
        part_number="250000059", part_name="SCREW OLD", retail_price_usd=Decimal("0")
    )
    priced = PolarisCatalogPart.objects.create(
        part_number="250000418", part_name="SCREW NEW",
        superseded_number="250000059", retail_price_usd=Decimal("4.19"),
    )
    found = find_polaris_by_number("250000059")
    assert found == exact
    assert find_polaris_price_source("250000059", found) == priced
    assert effective_customer_price_rub("250000059", found) == Decimal("616")


def test_core_search_shows_polaris_and_brp_collision(client, make_user, db):
    BrpCatalogPart.objects.create(material_no="12345", part_desc="BRP FILTER")
    PolarisCatalogPart.objects.create(part_number="12345", part_name="POLARIS FILTER")
    _login(client, make_user, superuser=True)
    html = client.get(reverse("part_search"), {"q": "12345"}).content.decode()
    assert "Найдено в BRP-каталоге" in html
    assert "Найдено в Polaris-каталоге" in html
    assert "BRP FILTER" in html
    assert "POLARIS FILTER" in html


def test_counting_accepts_polaris_scan_and_counts_value(db, admin):
    polaris = PolarisCatalogPart.objects.create(
        part_number="3610030", part_name="OIL SEAL", retail_price_usd=Decimal("10")
    )
    location = get_or_create_location("S01-L02-D03-C08")
    session = start_session(location=location, by=admin)
    line = record_scan(session, "3610030", by=admin)
    assert line.source == "polaris_catalog"
    assert line.polaris_catalog_part == polaris
    assert line.final_customer_price_rub == Decimal("1470")
    assert session.counters()["polaris"] == 1
    assert session.counters()["total_value"] == Decimal("1470")

    receipt = convert_to_receipt(session, by=admin)
    part = PolarisPartLink.objects.get(polaris_part=polaris).part
    assert session.lines.get().warehouse_part == part
    assert StockBalance.objects.count() == 0
    assert receipt.lines.get().part_type == part


def test_actions_sale_polaris_snapshots_exact_number_and_manufacturer(db, admin):
    polaris = PolarisCatalogPart.objects.create(
        part_number="420931285", part_name="OIL SEAL",
        superseded_number="420931284", retail_price_usd=Decimal("24.49"),
    )
    part = promote_to_warehouse(polaris, by=admin)
    location = StorageLocation.objects.create(
        name="Ячейка", code="S04-L03-D01-C04", storage_allowed=True, is_active=True
    )
    _stock(part, location, admin, qty="5")
    action = perform_action(
        part=part,
        location=location,
        action_type="sale",
        quantity="1",
        customer_comment="Рома",
        scanned_number="420931285",
        by=admin,
    )
    assert action.part_number == "420931285"
    assert action.manufacturer_name == "POLARIS"
    assert action.price_source_number == ""
    assert PartNumber.objects.filter(part=part, value="420931284").exists()


def test_actions_polaris_price_source_does_not_replace_identity(db, admin):
    exact = PolarisCatalogPart.objects.create(
        part_number="250000059", part_name="SCREW OLD", retail_price_usd=Decimal("0")
    )
    PolarisCatalogPart.objects.create(
        part_number="250000418", part_name="SCREW NEW",
        superseded_number="250000059", retail_price_usd=Decimal("4.19"),
    )
    part = promote_to_warehouse(exact, by=admin, manual_price=Decimal("616"))
    location = StorageLocation.objects.create(
        name="Ячейка", code="S01-L01-D01-C01", storage_allowed=True, is_active=True
    )
    _stock(part, location, admin, qty="2")
    action = perform_action(
        part=part,
        location=location,
        action_type="sale",
        quantity="1",
        customer_comment="Клиент",
        scanned_number="250000059",
        by=admin,
    )
    assert action.part_number == "250000059"
    assert action.price_source_number == "250000418"


def test_customs_export_polaris_exact_number_country_and_application(db, admin):
    polaris = PolarisCatalogPart.objects.create(
        part_number="420931285", part_name="OIL SEAL", retail_price_usd=Decimal("24.49")
    )
    part = promote_to_warehouse(polaris, by=admin)
    location = StorageLocation.objects.create(
        name="Ячейка", code="S04-L03-D01-C04", storage_allowed=True, is_active=True
    )
    _stock(part, location, admin, qty="5")
    perform_action(
        part=part,
        location=location,
        action_type="sale",
        quantity="2",
        customer_comment="Рома",
        scanned_number="420931285",
        by=admin,
    )
    sheet = openpyxl.load_workbook(export_customs_xlsx(actions_report()[0]))["Лист1"]
    assert str(sheet["B10"].value) == "420931285"
    assert sheet["D10"].value == "OIL SEAL"
    assert sheet["E10"].value == "POLARIS"
    assert sheet["F10"].value == "CANADA"  # страна всегда латиницей
    assert sheet["K10"].value is None  # оптовой цены в прайсе нет — розницу не подставляем
    assert sheet["M10"].value is None  # применимость не задана — категорию не выдумываем


def test_polaris_search_page_settings_gated_and_visible(client, make_user, db):
    PolarisCatalogPart.objects.create(
        part_number="3022082", part_name="GASKET", retail_price_usd=Decimal("12")
    )
    _login(client, make_user, role=roles.SELLER, name="seller")
    assert client.get(reverse("polaris_settings")).status_code == 403
    client.logout()
    _login(client, make_user, superuser=True, name="boss")
    html = client.get(reverse("polaris_search"), {"q": "3022082"}).content.decode()
    assert "Polaris-каталог" in html
    assert "GASKET" in html
    assert "1764" in html
    assert "—" not in html


def test_price_settings_refreshes_current_polaris_card_without_link_snapshot(
    client, make_user, db, admin
):
    polaris = PolarisCatalogPart.objects.create(
        part_number="5550001", part_name="SEAL", retail_price_usd=Decimal("100")
    )
    part = promote_to_warehouse(polaris, by=admin)
    link = part.polaris_link
    assert part.recommended_price == Decimal("14700")
    assert link.final_customer_price_rub == Decimal("14700")

    _login(client, make_user, superuser=True)
    resp = client.post(
        reverse("price_settings"),
        {
            "current_usd_rate": "120",
            "brp_markup_percent": "40",
            "polaris_markup_percent": "10",
        },
    )
    assert resp.status_code == 302

    part.refresh_from_db()
    link.refresh_from_db()
    assert part.recommended_price == Decimal("13200")
    assert link.usd_rate_used == Decimal("105")
    assert link.markup_percent_used == Decimal("40")
    assert link.final_customer_price_rub == Decimal("14700")
