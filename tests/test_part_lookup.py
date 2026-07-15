"""Canonical part identity lookup shared by search and warehouse scanners."""

from decimal import Decimal

import pytest

from apps.actions.services import perform_action
from apps.brp.models import BrpCatalogPart, BrpPartLink
from apps.catalog.models import Category, PartBarcode, PartNumber, PartType, Unit
from apps.core.part_lookup import MatchSource, resolve_part_lookup
from apps.counting.models import InventoryCountingLine
from apps.counting.services import start_session
from apps.inventory.models import PartItem, StockLot
from apps.polaris.models import PolarisCatalogPart, PolarisPartLink
from apps.procurement.models import Batch, BatchLine
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


def _part(category, unit, name, number):
    part = PartType.objects.create(
        name=name,
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("1250"),
    )
    PartNumber.objects.create(
        part=part,
        value=number,
        kind=PartNumber.Kind.OEM,
        is_primary=True,
    )
    return part


def _line(batch, part, quantity="10"):
    return BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("100"),
    )


@pytest.fixture
def lookup_data(db, django_user_model):
    category = Category.objects.create(name="Lookup")
    unit = Unit.objects.get(name="Штука")
    supplier = Supplier.objects.create(name="Lookup supplier")
    batch1 = Batch.objects.create(supplier=supplier)
    batch2 = Batch.objects.create(supplier=supplier)
    loc1 = StorageLocation.objects.create(
        name="Lookup 1", code="S11-L01-D01-C01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Lookup 2", code="S11-L01-D01-C02", storage_allowed=True, is_active=True
    )
    user = django_user_model.objects.create_superuser("lookup-admin", password="test-pass")

    brp = _part(category, unit, "BRP lookup", "BRP 420")
    PartBarcode.objects.create(part=brp, value="BRP-BARCODE")
    PartNumber.objects.create(part=brp, value="BRP 419", kind=PartNumber.Kind.ANALOG)
    brp_catalog = BrpCatalogPart.objects.create(
        material_no="BRP 420",
        part_desc="BRP lookup",
        replacement_no_1="BRP 419",
    )
    BrpPartLink.objects.create(
        part=brp,
        brp_part=brp_catalog,
        usd_rate_used=Decimal("100"),
        markup_percent_used=Decimal("40"),
    )

    polaris = _part(category, unit, "Polaris lookup", "POL 500")
    PartBarcode.objects.create(part=polaris, value="POL-BARCODE")
    PartNumber.objects.create(part=polaris, value="POL 400", kind=PartNumber.Kind.ANALOG)
    polaris_catalog = PolarisCatalogPart.objects.create(
        part_number="POL 500",
        part_name="Polaris lookup",
        superseded_number="POL 400",
    )
    PolarisPartLink.objects.create(
        part=polaris,
        polaris_part=polaris_catalog,
        usd_rate_used=Decimal("100"),
        markup_percent_used=Decimal("40"),
    )

    StockLot.objects.create(
        part_type=brp,
        batch=batch1,
        batch_line=_line(batch1, brp),
        location=loc1,
        quantity=Decimal("3"),
        initial_quantity=Decimal("3"),
        status=StockLot.Status.AVAILABLE,
    )
    StockLot.objects.create(
        part_type=brp,
        batch=batch2,
        batch_line=_line(batch2, brp),
        location=loc1,
        quantity=Decimal("2"),
        initial_quantity=Decimal("2"),
        status=StockLot.Status.AVAILABLE,
    )
    StockLot.objects.create(
        part_type=brp,
        batch=batch1,
        batch_line=_line(batch1, brp),
        location=loc2,
        quantity=Decimal("4"),
        initial_quantity=Decimal("4"),
        status=StockLot.Status.QUARANTINE,
    )
    return {
        "category": category,
        "unit": unit,
        "batch": batch1,
        "loc1": loc1,
        "loc2": loc2,
        "user": user,
        "brp": brp,
        "polaris": polaris,
    }


@pytest.mark.parametrize(
    ("value", "key", "number"),
    [("BRP 420", "brp", "BRP 420"), ("POL 500", "polaris", "POL 500")],
)
def test_exact_catalog_number_keeps_identity(lookup_data, value, key, number):
    result = resolve_part_lookup(value)
    assert result.found
    assert result.candidate.part == lookup_data[key]
    assert result.candidate.exact_number == number
    assert result.candidate.match_source == MatchSource.EXACT


@pytest.mark.parametrize(
    ("value", "key"),
    [("BRP-BARCODE", "brp"), ("POL-BARCODE", "polaris")],
)
def test_catalog_barcode_resolves_exact_part(lookup_data, value, key):
    result = resolve_part_lookup(value)
    assert result.found
    assert result.candidate.part == lookup_data[key]
    assert result.candidate.match_source == MatchSource.BARCODE


def test_scanner_whitespace_crlf_and_nbsp_are_normalized(lookup_data):
    result = resolve_part_lookup("\u00a0 BRP\u00a0420\r\n")
    assert result.found
    assert result.candidate.exact_number == "BRP 420"


def test_brp_replacement_is_explicit_alias(lookup_data):
    candidate = resolve_part_lookup("BRP 419").candidate
    assert candidate.part == lookup_data["brp"]
    assert candidate.exact_number == "BRP 420"
    assert candidate.match_source == MatchSource.REPLACEMENT
    assert candidate.alias_message == "Найдено по заменённому номеру: BRP 419"


def test_polaris_superseded_is_explicit_alias(lookup_data):
    candidate = resolve_part_lookup("POL 400").candidate
    assert candidate.part == lookup_data["polaris"]
    assert candidate.exact_number == "POL 500"
    assert candidate.match_source == MatchSource.SUPERSEDED


def test_ambiguous_alias_never_selects_random_part(lookup_data):
    second = _part(lookup_data["category"], lookup_data["unit"], "Other", "OTHER 1")
    PartNumber.objects.create(part=second, value="BRP 419", kind=PartNumber.Kind.ANALOG)
    result = resolve_part_lookup("BRP 419")
    assert result.ambiguous
    assert result.candidate is None
    assert {row.part for row in result.candidates} == {lookup_data["brp"], second}


def test_name_lookup_has_live_cells_and_per_cell_quantities(lookup_data):
    candidate = resolve_part_lookup("BRP lookup", allow_name=True).candidate
    assert candidate.locations == ["S11-L01-D01-C01", "S11-L01-D01-C02"]
    assert candidate.physical == Decimal("9")
    assert candidate.available == Decimal("5")
    assert candidate.quarantine == Decimal("4")
    assert candidate.location_rows[0].physical == Decimal("5")


def test_reservation_is_separate_from_available(lookup_data):
    perform_action(
        part=lookup_data["brp"],
        location=lookup_data["loc1"],
        action_type="reserve",
        quantity="2",
        customer_comment="Lookup reserve",
        by=lookup_data["user"],
        request_token="lookup-reserve",
    )
    candidate = resolve_part_lookup("BRP 420").candidate
    assert candidate.physical == Decimal("9")
    assert candidate.reserved == Decimal("2")
    assert candidate.available == Decimal("3")
    assert candidate.quarantine == Decimal("4")


def test_historical_counting_line_is_not_live_stock(lookup_data):
    historical = _part(
        lookup_data["category"], lookup_data["unit"], "Historical", "HISTORY 99"
    )
    session = start_session(location=lookup_data["loc1"], by=lookup_data["user"])
    InventoryCountingLine.objects.create(
        session=session,
        scanned_value="HISTORY 99",
        normalized_value="HISTORY99",
        warehouse_part=historical,
        display_name="Historical",
        source=InventoryCountingLine.Source.WAREHOUSE,
        quantity_counted=Decimal("99"),
        scan_count=99,
    )
    candidate = resolve_part_lookup("HISTORY 99").candidate
    assert candidate.physical == Decimal("0")
    assert candidate.locations == []


@pytest.mark.parametrize("status", [PartItem.Status.SOLD, PartItem.Status.WRITTEN_OFF])
def test_non_physical_item_status_is_not_current_stock(lookup_data, status):
    serial = _part(lookup_data["category"], lookup_data["unit"], status, f"SERIAL {status}")
    line = _line(lookup_data["batch"], serial)
    PartItem.objects.create(
        internal_number=f"DS-{status}",
        part_type=serial,
        batch=lookup_data["batch"],
        batch_line=line,
        status=status,
        current_location=lookup_data["loc1"],
    )
    candidate = resolve_part_lookup(f"DS-{status}").candidate
    assert candidate.part == serial
    assert candidate.physical == Decimal("0")
    assert candidate.locations == []


def test_lookup_query_count_is_bounded_for_multiple_results(
    lookup_data, django_assert_max_num_queries
):
    for index in range(8):
        _part(
            lookup_data["category"],
            lookup_data["unit"],
            f"Bounded lookup {index}",
            f"BOUND {index}",
        )
    with django_assert_max_num_queries(18):
        result = resolve_part_lookup("Bounded lookup", allow_name=True)
        assert len(result.candidates) == 8
