"""Stage 8 — операторский адрес 1-1-1.

Это ИЗМЕНЕНИЕ ПРЕДСТАВЛЕНИЯ, а не новая схема адресов. Хранимый code, штрихкод,
первичный ключ, привязки лотов и деталей, остатки и исторические снимки
остаются прежними: место то же самое, показывается иначе.

Старая буквенная форма продолжает находить ту же ячейку: она напечатана на
ярлыках, в распечатках и в документах, и перестать работать не может.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockBalance, StockLot
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import (
    AddressError,
    get_or_create_location,
    normalize_address_input,
    parse_short_address,
    short_address,
)
from apps.warehouse.models import StorageLocation
from apps.warehouse.services import resolve_storage_location

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


@pytest.fixture
def env(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    loc = get_or_create_location("S02-D03-C01", name="Ячейка")
    part = PartType.objects.create(
        name="Болт", category=Category.objects.create(name="Вариатор"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    PartNumber.objects.create(part=part, value="700100", kind=PartNumber.Kind.OEM)
    PartCustomsInfo.objects.create(
        part_type=part, gross_weight_kg=Decimal("0.350"), net_weight_kg=Decimal("0.300"),
        application_area=PartCustomsInfo.ApplicationArea.SNOWMOBILE,
    )
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal("10"), unit_cost_currency=Decimal("1")
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, loc, Decimal("10"))
    receive_stock_lot(lot, by=admin)
    return {"sup": sup, "admin": admin, "loc": loc, "part": part, "lot": lot}


def _login(client, make_user, name="boss"):
    make_user(name, is_superuser=True)
    client.login(username=name, password=PASSWORD)


# --- Формат ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stored,shown",
    [
        ("S01-D01-C01", "1-1-1"),
        ("S02-D03-C01", "2-3-1"),
        ("S10-D11-C12", "10-11-12"),
        ("S01-D01", "1-1"),
        ("S01", "1"),
    ],
)
def test_stored_code_is_shown_in_the_operator_format(stored, shown):
    assert short_address(stored) == shown


def test_cell_zero_is_preserved():
    """Ноль на складе существует и подменять его нечем."""
    assert short_address("S01-D01-C00") == "1-1-0"
    assert short_address("S01-D00-C01") == "1-0-1"


def test_a_legacy_address_is_not_rewritten():
    """У старого S-L адреса короткой формы нет: показать её значило бы солгать."""
    assert short_address("S04-L03-D01-C04") == "S04-L03-D01-C04"
    assert short_address("A-S01-L02-K03") == "A-S01-L02-K03"


def test_empty_stays_empty():
    assert short_address("") == ""
    assert short_address(None) == ""


@pytest.mark.parametrize(
    "typed,stored",
    [("1-1-1", "S01-D01-C01"), ("2-3-1", "S02-D03-C01"), ("10-11-12", "S10-D11-C12")],
)
def test_the_operator_format_normalizes_back_to_the_stored_code(typed, stored):
    assert normalize_address_input(typed) == stored


def test_the_stored_form_is_still_accepted_unchanged():
    assert normalize_address_input("S02-D03-C01") == "S02-D03-C01"


def test_a_cell_outside_a_drawer_is_still_refused():
    with pytest.raises(AddressError):
        parse_short_address("1--5")


# --- Идентичность места -------------------------------------------------------------------


def test_both_forms_resolve_to_the_same_location(env):
    short, _ = resolve_storage_location("2-3-1")
    long, _ = resolve_storage_location("S02-D03-C01")
    assert short is not None
    assert short.pk == long.pk == env["loc"].pk


def test_the_operator_format_creates_no_duplicate_location(env):
    before = StorageLocation.objects.count()
    resolve_storage_location("2-3-1")
    assert StorageLocation.objects.count() == before
    assert StorageLocation.objects.filter(code="S02-D03-C01").count() == 1
    assert not StorageLocation.objects.filter(code="2-3-1").exists()


def test_the_stored_identity_is_untouched(env):
    loc = StorageLocation.objects.get(pk=env["loc"].pk)
    assert loc.code == "S02-D03-C01"
    assert loc.barcode == "LOC:S02-D03-C01"
    assert loc.short_code == "2-3-1"


def test_the_known_active_cell_is_neither_deleted_nor_archived(env):
    loc = StorageLocation.objects.get(code="S02-D03-C01")
    assert loc.is_active
    assert loc.pk == env["loc"].pk


def test_lot_and_part_bindings_survive(env):
    lot = StockLot.objects.get(pk=env["lot"].pk)
    assert lot.location_id == env["loc"].pk
    assert lot.part_type_id == env["part"].pk


def test_stock_is_unchanged(env):
    balances = StockBalance.objects.filter(part_type=env["part"])
    assert sum(b.quantity_physical for b in balances) == Decimal("10")


# --- Экраны ------------------------------------------------------------------------------


def test_search_shows_the_operator_format(client, make_user, env):
    _login(client, make_user)
    body = client.get(reverse("part_search") + "?q=700100").content.decode()
    assert "2-3-1" in body
    assert "S02-D03-C01" not in body


def test_scanning_the_operator_format_finds_the_cell(client, make_user, env):
    _login(client, make_user)
    resp = client.get(reverse("scanner_move_locations") + "?q=2-3-1")
    results = resp.json()["results"]
    assert [row["id"] for row in results] == [env["loc"].pk]
    assert results[0]["code"] == "2-3-1"


def test_scanning_the_stored_format_still_finds_the_cell(client, make_user, env):
    _login(client, make_user)
    resp = client.get(reverse("scanner_move_locations") + "?q=S02-D03-C01")
    assert [row["id"] for row in resp.json()["results"]] == [env["loc"].pk]


def test_quick_actions_shows_the_operator_format(client, make_user, env):
    _login(client, make_user)
    body = client.get(reverse("actions_scan") + "?q=700100&kind=sale").content.decode()
    assert "2-3-1" in body
    assert "S02-D03-C01" not in body


def test_quick_actions_still_completes_through_the_cell(client, make_user, env):
    _login(client, make_user)
    client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "sale", "quantity": "1", "q": "700100",
    })
    client.post(reverse("actions_cart_complete"), {
        "kind": "sale", "customer_id": Customer.objects.create(name="Иванов").pk, "q": "700100",
    }, follow=True)
    action = WarehouseAction.objects.get()
    assert action.location_id == env["loc"].pk
    # Снимок адреса в журнале остаётся хранимой формой: это исторический факт.
    assert action.location_code == "S02-D03-C01"


def test_the_action_journal_shows_the_snapshot_in_the_operator_format(client, make_user, env):
    _login(client, make_user)
    client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "sale", "quantity": "1", "q": "700100",
    })
    client.post(reverse("actions_cart_complete"), {
        "kind": "sale", "customer_id": Customer.objects.create(name="Иванов").pk, "q": "700100",
    }, follow=True)
    body = client.get(reverse("actions_report")).content.decode()
    assert "2-3-1" in body


def test_the_action_report_filter_accepts_both_forms(client, make_user, env):
    _login(client, make_user)
    client.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": "sale", "quantity": "1", "q": "700100",
    })
    client.post(reverse("actions_cart_complete"), {
        "kind": "sale", "customer_id": Customer.objects.create(name="Иванов").pk, "q": "700100",
    }, follow=True)
    for term in ("2-3-1", "S02-D03-C01"):
        body = client.get(reverse("actions_report") + f"?location_code={term}").content.decode()
        assert "700100" in body, term


def test_inventory_balances_show_the_operator_format(client, make_user, env):
    _login(client, make_user)
    body = client.get(reverse("balance_list")).content.decode()
    assert "2-3-1" in body
    assert "S02-D03-C01" not in body


def test_lot_list_shows_the_operator_format(client, make_user, env):
    _login(client, make_user)
    body = client.get(reverse("lot_list")).content.decode()
    assert "2-3-1" in body


def test_the_printed_label_shows_the_operator_format_and_keeps_its_barcode(
    client, make_user, env
):
    _login(client, make_user)
    body = client.get(reverse("label_location", args=[env["loc"].pk])).content.decode()
    assert "2-3-1" in body
    assert "LOC:S02-D03-C01" in body  # штрихкод продолжает сканироваться


def test_cancellation_preview_shows_the_operator_format(client, make_user, env):
    from apps.sales.services import (
        add_stock_lot_to_sale,
        complete_sale,
        create_sale,
    )

    _login(client, make_user)
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_sale(sale, env["lot"], Decimal("2"), unit_price=Decimal("500"),
                          by=env["admin"])
    complete_sale(sale, by=env["admin"])
    body = client.get(reverse("sale_cancel_confirm", args=[sale.pk])).content.decode()
    assert "Куда вернётся товар" in body
    assert "2-3-1" in body
    assert "S02-D03-C01" not in body
