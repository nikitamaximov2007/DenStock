"""Strict part identity regressions for scanner-first warehouse operations."""

from decimal import Decimal

import openpyxl
import pytest
from django.urls import reverse

from apps.actions.models import WarehouseAction
from apps.actions.services import (
    ActionError,
    actions_report,
    cancel_warehouse_action,
    export_customs_xlsx,
    perform_action,
    resolve_part,
)
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.core.part_lookup import MatchSource, resolve_part_lookup
from apps.core.receiving_queue import find_receiving_candidates
from apps.core.scanner import resolve_scan
from apps.counting.models import InventoryCountingLine
from apps.counting.services import record_scan, start_session
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

EXACT = "512061507"
RELATED_A = "512060448"
RELATED_B = "512060387"
RELATED_ONLY = "512061506"
PASSWORD = "exact-action-pass"


def _stock(*, part, location, supplier, quantity, by):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, by)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal(quantity))
    receive_stock_lot(lot, by=by)
    return lot


@pytest.fixture
def exact_action_data(db, django_user_model):
    admin = django_user_model.objects.create_superuser(
        username="exact-action-admin",
        password=PASSWORD,
    )
    supplier = Supplier.objects.create(name="Exact action supplier")
    Unit.objects.get(name="Штука")
    location_a = StorageLocation.objects.create(
        name="Exact action A",
        code="S12-L01-D01-C01",
        storage_allowed=True,
        is_active=True,
    )
    location_b = StorageLocation.objects.create(
        name="Exact action B",
        code="S12-L01-D01-C02",
        storage_allowed=True,
        is_active=True,
    )

    exact_catalog = BrpCatalogPart.objects.create(
        material_no=EXACT,
        part_desc="DAMPER, VIBRATION",
        retail_price_usd=Decimal("15"),
        wholesale_price_usd=Decimal("12"),
    )
    related_catalog_a = BrpCatalogPart.objects.create(
        material_no=RELATED_A,
        part_desc="RUBBER MOUNT",
        replacement_no_1=EXACT,
        replacement_no_2=RELATED_ONLY,
        retail_price_usd=Decimal("8"),
    )
    related_catalog_b = BrpCatalogPart.objects.create(
        material_no=RELATED_B,
        part_desc="RUBBER MOUNT",
        replacement_no_1=EXACT,
        retail_price_usd=Decimal("7"),
    )
    exact_part = promote_to_warehouse(exact_catalog, by=admin)
    related_part_a = promote_to_warehouse(related_catalog_a, by=admin)
    related_part_b = promote_to_warehouse(related_catalog_b, by=admin)

    exact_lot = _stock(
        part=exact_part,
        location=location_a,
        supplier=supplier,
        quantity="6",
        by=admin,
    )
    related_lot_a = _stock(
        part=related_part_a,
        location=location_a,
        supplier=supplier,
        quantity="3",
        by=admin,
    )
    related_lot_b = _stock(
        part=related_part_b,
        location=location_a,
        supplier=supplier,
        quantity="4",
        by=admin,
    )
    return {
        "admin": admin,
        "supplier": supplier,
        "location_a": location_a,
        "location_b": location_b,
        "exact_part": exact_part,
        "related_part_a": related_part_a,
        "related_part_b": related_part_b,
        "exact_lot": exact_lot,
        "related_lot_a": related_lot_a,
        "related_lot_b": related_lot_b,
    }


def test_canonical_lookup_exact_identity_excludes_related_numbers(exact_action_data):
    result = resolve_part_lookup(EXACT)

    assert result.found
    assert result.candidate.part == exact_action_data["exact_part"]
    assert result.candidate.exact_number == EXACT
    assert result.candidate.match_source == MatchSource.EXACT
    assert [candidate.exact_number for candidate in result.candidates] == [EXACT]


def test_alias_only_number_is_not_an_operation_identity(exact_action_data):
    strict = resolve_part_lookup(RELATED_ONLY)
    reference = resolve_part_lookup(RELATED_ONLY, allow_alias=True)

    assert strict.status == "not_found"
    assert reference.found
    assert reference.candidate.part == exact_action_data["related_part_a"]
    assert reference.candidate.is_alias is True


@pytest.mark.parametrize("raw", [EXACT, " 512 061 507\r\n"])
def test_scanner_and_action_resolvers_keep_exact_identity(exact_action_data, raw):
    lookup = resolve_scan(raw)

    assert lookup.status == "found"
    assert lookup.type == "part_type"
    assert lookup.id == exact_action_data["exact_part"].pk
    assert lookup.exact_number == EXACT
    assert resolve_part(raw) == exact_action_data["exact_part"]


def test_scanner_does_not_promote_alias_only_number(exact_action_data):
    result = resolve_scan(RELATED_ONLY)

    assert result.status == "unknown"
    assert resolve_part(RELATED_ONLY) is None


def test_scanner_resolve_endpoint_keeps_exact_and_rejects_alias(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])

    exact = client.post(reverse("scanner_resolve"), {"code": EXACT}).json()
    related = client.post(reverse("scanner_resolve"), {"code": RELATED_ONLY}).json()

    assert exact["found"] is True
    assert exact["id"] == exact_action_data["exact_part"].pk
    assert exact["label"] == "DAMPER, VIBRATION"
    assert exact["candidates"] == []
    assert related["found"] is False
    assert related["status"] == "unknown"


def test_actions_page_shows_only_exact_production_number(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])
    html = client.get(reverse("actions_scan"), {"q": EXACT}).content.decode()

    assert EXACT in html
    assert "DAMPER, VIBRATION" in html
    assert "BRP" in html
    assert RELATED_A not in html
    assert RELATED_B not in html
    assert "Найдено несколько складских карточек" not in html
    assert "Провести действие" in html


def test_alias_only_action_scan_is_not_an_operation_candidate(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])
    html = client.get(reverse("actions_scan"), {"q": RELATED_ONLY}).content.decode()

    assert "Деталь не найдена в остатках склада." in html
    assert "Провести действие" not in html
    assert RELATED_A not in html


def test_multiple_locations_of_same_exact_part_remain_available(
    client,
    exact_action_data,
):
    _stock(
        part=exact_action_data["exact_part"],
        location=exact_action_data["location_b"],
        supplier=exact_action_data["supplier"],
        quantity="2",
        by=exact_action_data["admin"],
    )
    client.force_login(exact_action_data["admin"])
    html = client.get(reverse("actions_scan"), {"q": EXACT}).content.decode()

    assert "S12-L01-D01-C01" in html
    assert "S12-L01-D01-C02" in html
    assert "Деталь найдена в нескольких ячейках" in html
    assert "Найдено несколько складских карточек" not in html
    assert RELATED_A not in html
    assert RELATED_B not in html


def test_same_exact_number_on_two_real_cards_requires_explicit_part_choice(
    client,
    exact_action_data,
):
    duplicate = PartType.objects.create(
        name="Second exact identity",
        category=exact_action_data["exact_part"].category,
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=duplicate,
        value=EXACT,
        kind=PartNumber.Kind.OEM,
        is_primary=True,
    )
    _stock(
        part=duplicate,
        location=exact_action_data["location_a"],
        supplier=exact_action_data["supplier"],
        quantity="2",
        by=exact_action_data["admin"],
    )
    client.force_login(exact_action_data["admin"])

    ambiguous = client.get(reverse("actions_scan"), {"q": EXACT}).content.decode()
    selected = client.get(
        reverse("actions_scan"),
        {"q": EXACT, "part_id": exact_action_data["exact_part"].pk},
    ).content.decode()

    assert "Найдено несколько складских карточек" in ambiguous
    assert f"part_id={exact_action_data['exact_part'].pk}" in ambiguous
    assert f"part_id={duplicate.pk}" in ambiguous
    assert "DAMPER, VIBRATION" in selected
    assert "Провести действие" in selected


def test_actions_post_rejects_part_id_that_does_not_match_exact_scan(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])
    before = exact_action_data["related_lot_a"].quantity
    response = client.post(
        reverse("actions_perform"),
        {
            "part_id": exact_action_data["related_part_a"].pk,
            "location_id": exact_action_data["location_a"].pk,
            "action_type": WarehouseAction.Type.SALE,
            "quantity": "1",
            "customer_comment": "Tampered identity",
            "q": EXACT,
        },
        follow=True,
    )

    exact_action_data["related_lot_a"].refresh_from_db()
    assert exact_action_data["related_lot_a"].quantity == before
    assert WarehouseAction.objects.count() == 0
    assert "не соответствует выбранной детали" in response.content.decode()


def test_action_service_rejects_alias_as_scanned_identity(exact_action_data):
    before = exact_action_data["related_lot_a"].quantity

    with pytest.raises(ActionError, match="не соответствует выбранной детали"):
        perform_action(
            part=exact_action_data["related_part_a"],
            location=exact_action_data["location_a"],
            action_type=WarehouseAction.Type.SALE,
            quantity="1",
            customer_comment="Alias only",
            scanned_number=RELATED_ONLY,
            by=exact_action_data["admin"],
        )

    exact_action_data["related_lot_a"].refresh_from_db()
    assert exact_action_data["related_lot_a"].quantity == before
    assert WarehouseAction.objects.count() == 0


def test_receiving_candidates_do_not_mix_replacement_cards(exact_action_data):
    candidates = find_receiving_candidates(EXACT)

    assert {candidate.exact_number for candidate in candidates} == {EXACT}
    assert {candidate.part_id for candidate in candidates} == {
        exact_action_data["exact_part"].pk
    }
    assert find_receiving_candidates(RELATED_ONLY) == []


def test_scanner_receiving_endpoint_does_not_queue_alias(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])
    response = client.post(
        reverse("scanner_receiving"),
        {"action": "scan", "code": RELATED_ONLY},
    )

    assert response.status_code == 200
    assert "не найден в каталогах BRP, Polaris" in response.content.decode()
    assert not client.session.get("batch_receiving_queue_v1", {"lines": {}})["lines"]


def test_movement_endpoint_accepts_exact_and_rejects_alias(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])

    exact = client.post(reverse("scanner_move"), {"action": "scan", "code": EXACT})
    related = client.post(
        reverse("scanner_move"),
        {"action": "scan", "code": RELATED_ONLY},
    )

    assert exact.context["object"].part_type == exact_action_data["exact_part"]
    assert related.context["object"] is None
    assert related.context["error"] == "Код не распознан."


def test_counting_exact_scan_uses_only_exact_warehouse_part(exact_action_data):
    session = start_session(
        location=exact_action_data["location_a"],
        by=exact_action_data["admin"],
    )
    line = record_scan(session, EXACT, by=exact_action_data["admin"])
    related = record_scan(session, RELATED_ONLY, by=exact_action_data["admin"])

    assert line.source == InventoryCountingLine.Source.WAREHOUSE
    assert line.warehouse_part == exact_action_data["exact_part"]
    assert related.source == InventoryCountingLine.Source.UNKNOWN
    assert related.warehouse_part is None


def test_general_search_exact_result_does_not_mix_related_numbers(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])
    html = client.get(reverse("part_search"), {"q": EXACT}).content.decode()

    assert EXACT in html
    assert "DAMPER, VIBRATION" in html
    assert RELATED_A not in html
    assert RELATED_B not in html


def test_general_search_labels_aliases_as_related_reference_data(
    client,
    exact_action_data,
):
    client.force_login(exact_action_data["admin"])
    html = client.get(reverse("part_search"), {"q": RELATED_ONLY}).content.decode()

    assert "Возможные связанные номера" in html
    assert RELATED_A in html
    assert "Провести действие" not in html


def test_sale_cancel_report_and_export_keep_exact_snapshot(
    client,
    exact_action_data,
):
    first = perform_action(
        part=exact_action_data["exact_part"],
        location=exact_action_data["location_a"],
        action_type=WarehouseAction.Type.SALE,
        quantity="1",
        customer_comment="Cancel exact",
        scanned_number=EXACT,
        by=exact_action_data["admin"],
    )
    assert first.part_number == EXACT
    cancel_warehouse_action(first, by=exact_action_data["admin"], reason="Regression")
    first.refresh_from_db()
    assert first.part_number == EXACT

    active = perform_action(
        part=exact_action_data["exact_part"],
        location=exact_action_data["location_a"],
        action_type=WarehouseAction.Type.SALE,
        quantity="2",
        customer_comment="Active exact",
        scanned_number=EXACT,
        by=exact_action_data["admin"],
    )
    assert active.part_number == EXACT

    actions, _totals = actions_report()
    assert list(actions.values_list("part_number", flat=True)) == [EXACT]
    workbook = openpyxl.load_workbook(export_customs_xlsx(actions))
    numbers = [
        str(workbook["Лист1"][f"B{row}"].value)
        for row in range(10, 15)
        if workbook["Лист1"][f"B{row}"].value
    ]
    assert EXACT in numbers
    assert RELATED_A not in numbers
    assert RELATED_B not in numbers

    client.force_login(exact_action_data["admin"])
    report_html = client.get(reverse("actions_report")).content.decode()
    assert EXACT in report_html
    assert RELATED_A not in report_html
    assert RELATED_B not in report_html


def test_leading_zero_identity_is_preserved(exact_action_data):
    part = PartType.objects.create(
        name="Leading zero",
        category=Category.objects.create(name="Leading zero category"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=part,
        value="001-230",
        kind=PartNumber.Kind.OEM,
        is_primary=True,
    )

    result = resolve_part_lookup("001 230")

    assert result.found
    assert result.candidate.part == part
    assert result.candidate.exact_number == "001-230"
