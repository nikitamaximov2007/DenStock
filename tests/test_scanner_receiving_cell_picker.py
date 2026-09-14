"""Receiving cell picker: visible choices, quick selection, and no stale block."""

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext

from apps.accounts import roles
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.core.receiving_queue import QUEUE_SESSION_KEY, receiving_location_options
from apps.inventory.models import FoundStockPosting, PartPreferredLocation, StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.receipts.models import Receipt
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
URL = "/scanner/receiving/"
LOCATIONS_URL = "/scanner/receiving/locations/"
BLOCK = "Нужно выбрать ячейку"
PART_NUMBER = "420832590"


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


def _cell(code):
    return StorageLocation.objects.create(
        code=code, name=f"Ячейка {code}", storage_allowed=True, is_active=True
    )


def _stock(part, location, qty, supplier, admin):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(str(qty)),
        unit_cost_currency=Decimal("100"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(str(qty)))
    receive_stock_lot(lot, by=admin)
    return lot


@pytest.fixture
def warehouse(make_user):
    """The production shape: one part physically in two cells (50 and 1)."""
    admin = make_user("admin", is_superuser=True)
    supplier = Supplier.objects.create(name="Стартовый ввод")
    cells = {code: _cell(code) for code in ("S03-D02", "S07-D01", "S02-D01", "S10-D01", "S09-D01")}
    StorageLocation.objects.create(
        code="S01-D01", name="Архив", storage_allowed=True, is_active=False
    )
    StorageLocation.objects.create(
        code="S01-D02", name="Служебная", storage_allowed=False, is_active=True
    )
    brp = BrpCatalogPart.objects.create(
        material_no=PART_NUMBER, part_desc="BALL BEARING", retail_price_usd=Decimal("40")
    )
    part = promote_to_warehouse(brp, by=admin)
    _stock(part, cells["S03-D02"], 50, supplier, admin)
    _stock(part, cells["S07-D01"], 1, supplier, admin)
    assert PartPreferredLocation.objects.filter(part_type=part).update(location=cells["S09-D01"])
    return {"admin": admin, "supplier": supplier, "cells": cells, "part": part}


def _login(client, make_user, *, role=roles.STOREKEEPER, name="sklad"):
    make_user(name, role=role)
    client.login(username=name, password=PASSWORD)


def _queue(client):
    return client.session[QUEUE_SESSION_KEY]


def _lines(client):
    return list(_queue(client)["lines"].values())


def _single_line(client):
    lines = _lines(client)
    assert len(lines) == 1
    return lines[0]


def _scan(client, code=PART_NUMBER):
    return client.post(URL, {"action": "scan", "code": code})


def _assign(client, line_id, **fields):
    return client.post(URL, {"action": "queue_assign", "line_id": line_id, **fields})


def _codes(response):
    return [row["code"] for row in response.json()["results"]]


def _stock_snapshot(part):
    return (
        StockMovement.objects.count(),
        Receipt.objects.count(),
        FoundStockPosting.objects.count(),
        sorted(StockLot.objects.filter(part_type=part).values_list("location_id", "quantity")),
    )


def _write_queue(client, queue):
    session = client.session
    session[QUEUE_SESSION_KEY] = queue
    session.save()


# --- Picker endpoint ------------------------------------------------------------------


def test_picker_lists_cells_without_typed_query_existing_first(client, make_user, warehouse):
    _login(client, make_user)

    response = client.get(LOCATIONS_URL, {"q": "", "part": warehouse["part"].pk})

    assert response.status_code == 200
    payload = response.json()
    # Existing cells (natural order), then the preferred cell, then the rest with
    # 2-1 before 10-1; archived and non-storage cells never appear.
    assert _codes(response) == ["3-2", "7-1", "9-1", "2-1", "10-1"]
    assert [row["group"] for row in payload["results"]] == [
        "current", "current", "preferred", "other", "other",
    ]
    assert [row["physical"] for row in payload["results"]] == ["50", "1", "", "", ""]
    assert payload["truncated"] is False
    assert payload["total"] == 5


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("S", ["3-2", "7-1", "9-1", "2-1", "10-1"]),
        ("s03", ["3-2"]),
        ("S03-D02", ["3-2"]),
        ("s03-d02", ["3-2"]),
        ("3-2", ["3-2"]),
        ("D01", ["7-1", "9-1", "2-1", "10-1"]),
        ("d02", ["3-2"]),
        ("  s1\r\n", ["10-1"]),
        ("NO-SUCH-CELL", []),
    ],
)
def test_picker_typing_filters_case_insensitively(client, make_user, warehouse, query, expected):
    _login(client, make_user)
    response = client.get(LOCATIONS_URL, {"q": query, "part": warehouse["part"].pk})
    assert _codes(response) == expected


def test_picker_without_part_uses_natural_code_order(client, make_user, warehouse):
    _login(client, make_user)
    response = client.get(LOCATIONS_URL)
    assert _codes(response) == ["2-1", "3-2", "7-1", "9-1", "10-1"]
    assert {row["group"] for row in response.json()["results"]} == {"other"}


def test_picker_query_count_does_not_grow_with_cell_count(client, make_user, warehouse):
    _login(client, make_user)
    params = {"part": warehouse["part"].pk}
    client.get(LOCATIONS_URL, params)  # warm the session and auth caches
    with CaptureQueriesContext(connection) as small:
        client.get(LOCATIONS_URL, params)
    StorageLocation.objects.bulk_create(
        [
            StorageLocation(
                code=f"S20-D{number:02d}",
                barcode=f"LOC:S20-D{number:02d}",
                name=f"Ячейка {number}",
                storage_allowed=True,
                is_active=True,
            )
            for number in range(1, 131)
        ]
    )
    with CaptureQueriesContext(connection) as large:
        response = client.get(LOCATIONS_URL, params)

    assert len(response.json()["results"]) == 135
    assert _codes(response)[:3] == ["3-2", "7-1", "9-1"]
    assert len(large) == len(small)


def test_picker_limit_keeps_existing_cells_and_reports_truncation(warehouse):
    usable = StorageLocation.objects.filter(is_active=True, storage_allowed=True)
    payload = receiving_location_options(usable, warehouse["part"].pk, limit=3)
    assert [row["code"] for row in payload["results"]] == ["3-2", "7-1", "9-1"]
    assert payload["truncated"] is True
    assert payload["total"] == 5


def test_picker_endpoint_keeps_inventory_permissions(client, make_user, warehouse):
    assert client.get(LOCATIONS_URL).status_code == 302  # anonymous -> login

    _login(client, make_user, role=roles.SELLER, name="prodavec")
    assert client.get(LOCATIONS_URL).status_code == 403
    client.logout()

    _login(client, make_user, role=roles.VIEWER, name="nabludatel")
    assert client.get(LOCATIONS_URL).status_code == 403
    client.logout()

    _login(client, make_user)
    assert client.post(LOCATIONS_URL).status_code == 405
    assert client.get(LOCATIONS_URL).status_code == 200


# --- Blocking panel ---------------------------------------------------------------------


def test_blocking_panel_shows_cell_codes_and_quick_choices(client, make_user, warehouse):
    _login(client, make_user)
    html = client.post(URL, {"action": "scan", "code": PART_NUMBER}, follow=True).content.decode()

    line = _single_line(client)
    assert BLOCK in html
    assert [choice["short_code"] for choice in line["location_choices"]] == ["3-2", "7-1"]
    assert '<span class="code-pill">3-2</span>' in html
    assert '<span class="code-pill">7-1</span>' in html
    assert '<span class="code-pill">9-1</span>' in html  # preferred cell
    assert '<span class="code-pill"></span>' not in html
    # Two existing cells plus the preferred cell are one click away.
    assert html.count("data-receiving-quick-assign") == 3
    assert 'data-search-url="/scanner/receiving/locations/"' in html
    assert f'data-part-id="{warehouse["part"].pk}"' in html
    assert "move-destination__options--inline" in html
    assert "data-move-destination-clear" in html


def test_quick_assign_existing_cell_clears_block_without_stock_change(
    client, make_user, warehouse
):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    client.post(URL, {"action": "queue_update", "line_id": line["id"], "quantity": "4"})
    before = _stock_snapshot(warehouse["part"])
    target = warehouse["cells"]["S07-D01"]

    response = _assign(client, line["id"], location_id=target.pk)

    assert response.status_code == 302
    page = client.get(URL).content.decode()
    assert BLOCK not in page
    assert 'Ячейка <span class="code-pill">7-1</span>' in page
    assert "Деталь будет добавлена в 7-1." in page
    assigned = _single_line(client)
    assert assigned["id"] == line["id"]
    assert assigned["location_id"] == target.pk
    assert assigned["location_mode"] == "selected"
    assert assigned["quantity"] == 4
    assert assigned["unit_price"] == line["unit_price"]
    assert _stock_snapshot(warehouse["part"]) == before

    refreshed = client.get(URL).content.decode()
    assert BLOCK not in refreshed
    assert 'Ячейка <span class="code-pill">7-1</span>' in refreshed
    assert "Деталь будет добавлена" not in refreshed
    assert _single_line(client)["quantity"] == 4


@pytest.mark.parametrize(
    "fields",
    [
        {"location_code": "3-2"},
        {"location_code": "s03-d02"},
        {"location_id": "S03-D02", "location_code": "3-2"},
    ],
)
def test_picker_selection_is_stored_by_id_and_code(client, make_user, warehouse, fields):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    target = warehouse["cells"]["S03-D02"]
    if "location_id" in fields:
        fields = {**fields, "location_id": target.pk}

    assert _assign(client, line["id"], **fields).status_code == 302
    assert _single_line(client)["location_id"] == target.pk
    assert BLOCK not in client.get(URL).content.decode()


def test_preferred_cell_quick_assign(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    preferred = warehouse["cells"]["S09-D01"]
    assert _assign(client, line["id"], location_id=preferred.pk).status_code == 302
    assert _single_line(client)["location_id"] == preferred.pk
    assert 'Ячейка <span class="code-pill">9-1</span>' in client.get(URL).content.decode()


def test_rescan_after_choosing_cell_adds_to_that_line(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    target = warehouse["cells"]["S03-D02"]
    _assign(client, line["id"], location_id=target.pk)

    _scan(client)
    _scan(client)

    merged = _single_line(client)
    assert merged["id"] == line["id"]
    assert merged["location_id"] == target.pk
    assert merged["location_mode"] == "selected"
    assert merged["quantity"] == 3
    page = client.get(URL).content.decode()
    assert BLOCK not in page
    assert "К добавлению · 3 шт." in page


def test_rescan_stays_explicit_when_part_was_split_between_chosen_cells(
    client, make_user, warehouse
):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    _assign(client, line["id"], location_id=warehouse["cells"]["S03-D02"].pk)
    queue = _queue(client)
    twin = {
        **line,
        "id": "twin-line",
        "location_id": warehouse["cells"]["S07-D01"].pk,
        "location_mode": "selected",
        "created_order": 99,
    }
    queue["lines"][twin["id"]] = twin
    _write_queue(client, queue)

    _scan(client)

    unassigned = [row for row in _lines(client) if row["location_id"] is None]
    assert len(unassigned) == 1
    assert unassigned[0]["quantity"] == 1
    assert BLOCK in client.get(URL).content.decode()


# --- Failures keep the panel ----------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("inactive", "Выберите существующую активную ячейку"),
        ("unknown_code", "Ячейка с таким кодом не найдена"),
        ("deleted", "Выбранная ячейка больше не существует"),
        ("mismatch", "Код ячейки не соответствует выбранной ячейке"),
    ],
)
def test_failed_assignment_keeps_block_with_inline_error(
    client, make_user, warehouse, case, message
):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    before = _stock_snapshot(warehouse["part"])
    if case == "inactive":
        fields = {"location_id": StorageLocation.objects.get(code="S01-D01").pk}
    elif case == "unknown_code":
        fields = {"location_code": "NO-SUCH-CELL"}
    elif case == "deleted":
        gone = _cell("S30-D01")
        gone_id = gone.pk
        gone.delete()
        fields = {"location_id": gone_id}
    else:
        fields = {"location_id": warehouse["cells"]["S03-D02"].pk, "location_code": "7-1"}

    response = _assign(client, line["id"], **fields)

    html = response.content.decode()
    assert response.status_code == 200
    assert BLOCK in html
    assert "Деталь будет добавлена" not in html
    # Once in the page header and once inside the line that failed.
    assert html.count(message) == 2
    assert html.index(message, html.index('class="receiving-queue-line"')) > 0
    failed = _single_line(client)
    assert failed["location_id"] is None
    assert failed["quantity"] == 1
    assert _stock_snapshot(warehouse["part"]) == before


# --- Duplicate submits and two windows ----------------------------------------------------


def test_repeated_assign_submit_is_idempotent(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    target = warehouse["cells"]["S03-D02"]

    assert _assign(client, line["id"], location_id=target.pk).status_code == 302
    assert _assign(client, line["id"], location_id=target.pk).status_code == 302

    assigned = _single_line(client)
    assert assigned["location_id"] == target.pk
    assert assigned["quantity"] == 1


def test_second_window_choice_on_same_line_keeps_one_line(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)

    _assign(client, line["id"], location_id=warehouse["cells"]["S03-D02"].pk)
    _assign(client, line["id"], location_id=warehouse["cells"]["S07-D01"].pk)

    final = _single_line(client)
    assert final["location_id"] == warehouse["cells"]["S07-D01"].pk
    assert final["quantity"] == 1
    assert BLOCK not in client.get(URL).content.decode()


def test_stale_window_assign_after_merge_shows_current_state(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    first = _single_line(client)
    target = warehouse["cells"]["S03-D02"]
    _assign(client, first["id"], location_id=target.pk)
    # A second tab scanned the same part before the first tab's choice was saved.
    queue = _queue(client)
    stale = {
        **first,
        "id": "stale-line",
        "location_id": None,
        "location_mode": "multiple",
        "created_order": 99,
    }
    queue["lines"][stale["id"]] = stale
    _write_queue(client, queue)

    assert _assign(client, stale["id"], location_id=target.pk).status_code == 302
    merged = _single_line(client)
    assert merged["id"] == first["id"]
    assert merged["quantity"] == 2

    response = client.post(
        URL,
        {"action": "queue_assign", "line_id": stale["id"], "location_id": target.pk},
        follow=True,
    )
    html = response.content.decode()
    assert "уже изменена в другой вкладке" in html
    assert BLOCK not in html
    assert _single_line(client)["quantity"] == 2


def test_assign_from_stale_window_after_posting_does_not_touch_stock(
    client, make_user, warehouse
):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    target = warehouse["cells"]["S03-D02"]
    _assign(client, line["id"], location_id=target.pk)
    client.get(URL)
    token = _queue(client)["group_tokens"][str(target.pk)]["token"]
    assert (
        client.post(
            URL, {"action": "queue_post", "location_id": target.pk, "token": token}
        ).status_code
        == 302
    )
    after_post = _stock_snapshot(warehouse["part"])
    lot = StockLot.objects.get(part_type=warehouse["part"], location=target)
    assert lot.quantity == Decimal("51")

    response = _assign(client, line["id"], location_id=warehouse["cells"]["S07-D01"].pk)

    assert response.status_code == 302
    assert _lines(client) == []
    assert _stock_snapshot(warehouse["part"]) == after_post
    assert FoundStockPosting.objects.count() == 1


# --- Display safety and regressions ---------------------------------------------------------


def test_choice_without_cell_relation_is_labelled_not_blank(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    queue = _queue(client)
    line = next(iter(queue["lines"].values()))
    line["location_choices"][1] = {
        "id": None,
        "code": "",
        "short_code": "",
        "name": "",
        "physical": "1",
        "reserved": "0",
        "available": "1",
        "is_usable": False,
    }
    _write_queue(client, queue)

    html = client.get(URL).content.decode()

    assert "ячейка не указана" in html
    assert '<span class="code-pill"></span>' not in html
    assert html.count("data-receiving-quick-assign") == 2


def test_queue_saved_before_fix_still_shows_stored_code(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    queue = _queue(client)
    line = next(iter(queue["lines"].values()))
    for choice in line["location_choices"]:
        choice.pop("short_code")
    line["preferred_location"].pop("short_code")
    _write_queue(client, queue)

    html = client.get(URL).content.decode()

    assert '<span class="code-pill">S03-D02</span>' in html
    assert '<span class="code-pill">S07-D01</span>' in html
    assert '<span class="code-pill">9-1</span>' in html
    assert '<span class="code-pill"></span>' not in html


def test_regression_single_cell_part_needs_no_choice(client, make_user, warehouse):
    brp = BrpCatalogPart.objects.create(
        material_no="420999111", part_desc="SEAL", retail_price_usd=Decimal("5")
    )
    part = promote_to_warehouse(brp, by=warehouse["admin"])
    _stock(part, warehouse["cells"]["S02-D01"], 2, warehouse["supplier"], warehouse["admin"])
    _login(client, make_user)

    _scan(client, "420999111")

    line = _single_line(client)
    assert line["location_id"] == warehouse["cells"]["S02-D01"].pk
    assert line["location_mode"] == "current"
    html = client.get(URL).content.decode()
    assert BLOCK not in html
    assert 'Ячейка <span class="code-pill">2-1</span>' in html


def test_regression_part_without_previous_cells(client, make_user, warehouse):
    BrpCatalogPart.objects.create(
        material_no="420888222", part_desc="NEW GASKET", retail_price_usd=Decimal("7")
    )
    _login(client, make_user)
    lots_before = StockLot.objects.count()

    _scan(client, "420888222")

    line = _single_line(client)
    html = client.get(URL).content.decode()
    assert line["location_mode"] == "new"
    assert BLOCK in html
    assert "Ячейка не назначена" in html
    assert 'data-part-id=""' in html
    assert "receiving-current-cells" not in html

    assert _assign(client, line["id"], location_code="10-1").status_code == 302
    assert _single_line(client)["location_id"] == warehouse["cells"]["S10-D01"].pk
    assert BLOCK not in client.get(URL).content.decode()
    assert StockLot.objects.count() == lots_before


def test_regression_part_in_multiple_cells_requires_choice(client, make_user, warehouse):
    _login(client, make_user)
    _scan(client)
    line = _single_line(client)
    assert line["location_id"] is None
    assert line["location_mode"] == "multiple"
    assert "Сейчас деталь находится в нескольких ячейках" in client.get(URL).content.decode()


def test_picker_frontend_opens_on_focus_and_is_not_clipped(settings):
    javascript = (settings.BASE_DIR / "static" / "js" / "move_destination.js").read_text(
        encoding="utf-8"
    )
    stylesheet = (settings.BASE_DIR / "static" / "css" / "app.css").read_text(encoding="utf-8")

    for marker in (
        'input.addEventListener("focus"',
        'input.addEventListener("click"',
        'url.searchParams.set("part", partId)',
        'widget.hasAttribute("data-submit-on-enter")',
        "form.requestSubmit(button)",
        '"is-selected"',
        "[data-move-destination-clear]",
        'window.addEventListener("pageshow"',
        "GROUP_LABELS",
    ):
        assert marker in javascript
    assert ".move-destination__options--inline { position: static" in stylesheet
    assert ".receiving-queue-line .inline-form .btn--danger" in stylesheet
