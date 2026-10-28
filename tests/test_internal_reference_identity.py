"""Internal reference numbers identify existing cards in counting and receiving.

Real case: BRONCO PRODUCTS A-arm kit, article AT-04164, internal reference
number 127388 (the aftermarket supplier SKU printed on the package), zero
stock. The general search found it by 127388; the cell counting and the
scanner receiving answered «Неизвестная деталь», because the strict scanner
lookup checked only articles and factory barcodes.
"""

from decimal import Decimal
from html import escape

import pytest
from django.urls import reverse

from apps.catalog.models import (
    Category,
    Manufacturer,
    PartBarcode,
    PartNumber,
    PartType,
    Unit,
)
from apps.core.part_lookup import MatchSource, resolve_part_lookup
from apps.core.receiving_queue import QUEUE_SESSION_KEY, find_receiving_candidates
from apps.core.scanner import resolve_scan
from apps.core.search import search_parts
from apps.counting.models import InventoryCountingLine, InventoryScanEvent
from apps.counting.services import (
    convert_to_receipt,
    post_session,
    record_scan,
    start_session,
    undo_last_scan,
)
from apps.inventory.models import StockBalance, StockMovement
from apps.warehouse.addresses import get_or_create_location

NAME = "BRONCO A-ARM BEARING/BUSHING & SEAL KIT"


@pytest.fixture
def admin(db, django_user_model):
    return django_user_model.objects.create_superuser(username="admin", password="parol-12345")


@pytest.fixture
def unit(db):
    return Unit.objects.get(name="Штука")


@pytest.fixture
def category(db):
    return Category.objects.create(name="Подвеска")


def _card(name, category, unit, *, article, internal_ref=None, manufacturer=None):
    part = PartType.objects.create(
        name=name,
        category=category,
        unit=unit,
        manufacturer=manufacturer,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("4500"),
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    if internal_ref:
        PartNumber.objects.create(
            part=part,
            value=internal_ref,
            kind=PartNumber.Kind.INTERNAL_REF,
            note="SKU aftermarket-поставщика",
        )
    return part


@pytest.fixture
def bronco(db, category, unit):
    maker = Manufacturer.objects.create(name="BRONCO PRODUCTS")
    return _card(NAME, category, unit, article="AT-04164", internal_ref="127388",
                 manufacturer=maker)


@pytest.fixture
def location(db):
    return get_or_create_location("S01-D03-C08", name="Ящик 3 ячейка 8")


# --- Shared lookup -------------------------------------------------------------


@pytest.mark.parametrize("raw", ["127388", " 127388 ", "127388\n", "127388\r\n"])
def test_internal_reference_resolves_strictly_to_the_card(bronco, raw):
    result = resolve_part_lookup(raw)
    assert result.found
    candidate = result.candidate
    assert candidate.part == bronco
    assert candidate.match_source == MatchSource.INTERNAL_REF
    # The card's identity stays its manufacturer article, never the SKU.
    assert candidate.exact_number == "AT-04164"
    assert candidate.physical == Decimal("0")
    assert "внутреннему справочному номеру" in candidate.alias_message


@pytest.mark.parametrize("raw", ["AT-04164", "at04164", "AT 04164"])
def test_article_still_resolves_first(bronco, raw):
    result = resolve_part_lookup(raw)
    assert result.found
    assert result.candidate.part == bronco
    assert result.candidate.match_source == MatchSource.EXACT


def test_factory_barcode_still_resolves(bronco):
    PartBarcode.objects.create(part=bronco, value="0712345678901")
    result = resolve_part_lookup("0712345678901")
    assert result.found and result.candidate.part == bronco


def test_several_identifiers_of_one_card_are_not_a_conflict(bronco):
    # The card's article equals its own internal reference number.
    PartNumber.objects.create(part=bronco, value="AT04164", kind=PartNumber.Kind.INTERNAL_REF)
    result = resolve_part_lookup("AT-04164")
    assert result.found and result.candidate.part == bronco


def test_internal_reference_on_two_cards_is_ambiguous(bronco, category, unit):
    other = _card("OTHER KIT", category, unit, article="ZZ-1", internal_ref="127388")
    result = resolve_part_lookup("127388")
    assert result.ambiguous
    assert {c.part for c in result.candidates} == {bronco, other}


def test_article_of_one_card_and_reference_of_another_is_ambiguous(bronco, category, unit):
    other = _card("ANOTHER MAKER PART", category, unit, article="127388")
    result = resolve_part_lookup("127388")
    assert result.ambiguous
    sources = {c.part: c.match_source for c in result.candidates}
    assert sources == {other: MatchSource.EXACT, bronco: MatchSource.INTERNAL_REF}


def test_leading_zeros_are_kept(category, unit):
    part = _card("ZERO KIT", category, unit, article="ZK-1", internal_ref="007123")
    assert resolve_part_lookup("007123").candidate.part == part
    assert not resolve_part_lookup("7123").found
    assert resolve_part_lookup("7123").status == "not_found"


def test_hyphen_positions_keep_distinct_cards(category, unit):
    first = _card("FIRST", category, unit, article="010-090")
    second = _card("SECOND", category, unit, article="01-0090")
    assert resolve_part_lookup("010-090").candidate.part == first
    assert resolve_part_lookup("01-0090").candidate.part == second
    assert resolve_part_lookup("010090").ambiguous


def test_partial_lookup_does_not_promote_wrong_hyphen_layout(category, unit):
    _card("SECOND", category, unit, article="01-0090")
    result = resolve_part_lookup("010-090", allow_partial=True, allow_name=True)
    assert result.status == "not_found"
    assert not result.candidates


def test_internal_refs_keep_hyphen_positions(category, unit):
    first = _card("FIRST", category, unit, article="A-1", internal_ref="010-090")
    second = _card("SECOND", category, unit, article="A-2", internal_ref="01-0090")
    assert resolve_part_lookup("010-090").candidate.part == first
    assert resolve_part_lookup("01-0090").candidate.part == second
    assert resolve_part_lookup("010090").ambiguous


def test_article_and_other_cards_barcode_are_ambiguous(category, unit):
    first = _card("FIRST", category, unit, article="A-1")
    second = _card("SECOND", category, unit, article="B-2")
    PartBarcode.objects.create(part=second, value="A-1")
    result = resolve_part_lookup("A-1")
    assert result.ambiguous
    assert {candidate.part for candidate in result.candidates} == {first, second}


def test_unknown_number_stays_unknown(bronco):
    assert resolve_part_lookup("999999").status == "not_found"
    assert resolve_scan("999999").status == "unknown"


def test_internal_reference_is_not_a_partial_strict_match(bronco):
    # Strict identity is exact: a prefix of the SKU is not the card.
    assert resolve_part_lookup("12738").status == "not_found"


def test_scanner_and_general_search_agree(bronco):
    scan = resolve_scan("127388")
    assert scan.status == "found" and scan.type == "part_type" and scan.id == bronco.pk
    assert [c.part for c in search_parts("127388")] == [bronco]
    assert [c.part for c in search_parts("AT-04164")] == [bronco]


# --- Cell counting -------------------------------------------------------------


def test_counting_scan_by_internal_reference_finds_the_card(bronco, location, admin):
    session = start_session(location=location, by=admin)
    line = record_scan(session, "127388", by=admin)
    assert line.warehouse_part == bronco
    assert line.source != InventoryCountingLine.Source.UNKNOWN
    assert line.display_name == NAME
    assert line.final_customer_price_rub == Decimal("4500")


def test_counting_article_and_reference_count_on_one_line(bronco, location, admin):
    session = start_session(location=location, by=admin)
    record_scan(session, "AT-04164", by=admin)
    record_scan(session, "127388\n", by=admin)
    record_scan(session, "127388", by=admin)
    assert session.lines.count() == 1
    line = session.lines.get()
    assert line.warehouse_part == bronco
    assert line.quantity_counted == Decimal("3")
    assert InventoryScanEvent.objects.filter(session=session).count() == 3
    # Undo still takes back exactly the last scan from the shared line.
    assert undo_last_scan(session)
    line.refresh_from_db()
    assert line.quantity_counted == Decimal("2")


def test_counting_separates_articles_with_different_hyphen_positions(
    category, unit, location, admin
):
    first = _card("FIRST", category, unit, article="010-090")
    second = _card("SECOND", category, unit, article="01-0090")
    session = start_session(location=location, by=admin)
    assert record_scan(session, "010-090", by=admin).warehouse_part == first
    assert record_scan(session, "01-0090", by=admin).warehouse_part == second
    assert session.lines.count() == 2
    ambiguous = record_scan(session, "010090", by=admin)
    assert ambiguous.warehouse_part is None
    assert ambiguous.source == InventoryCountingLine.Source.UNKNOWN
    assert session.lines.count() == 3


def test_counting_ambiguous_reference_is_not_bound(bronco, location, admin, category, unit):
    _card("OTHER KIT", category, unit, article="ZZ-1", internal_ref="127388")
    session = start_session(location=location, by=admin)
    line = record_scan(session, "127388", by=admin)
    assert line.warehouse_part is None
    assert line.source == InventoryCountingLine.Source.UNKNOWN
    assert "нескольких складских карточек" in line.display_name


def test_counting_does_not_increment_old_line_after_reference_becomes_ambiguous(
    bronco, location, admin, category, unit
):
    session = start_session(location=location, by=admin)
    original = record_scan(session, "127388", by=admin)
    _card("OTHER KIT", category, unit, article="ZZ-1", internal_ref="127388")
    later = record_scan(session, "127388", by=admin)
    original.refresh_from_db()
    assert original.quantity_counted == Decimal("1")
    assert later.pk != original.pk
    assert later.warehouse_part is None
    assert later.source == InventoryCountingLine.Source.UNKNOWN


def test_counting_unknown_number_keeps_unknown_flow(bronco, location, admin):
    session = start_session(location=location, by=admin)
    line = record_scan(session, "999999", by=admin)
    assert line.source == InventoryCountingLine.Source.UNKNOWN
    assert line.display_name == "Неизвестная деталь"


def test_counting_post_adds_stock_to_existing_card_without_duplicates(
    bronco, location, admin
):
    parts_before = PartType.objects.count()
    session = start_session(location=location, by=admin)
    record_scan(session, "127388", by=admin)
    record_scan(session, "AT-04164", by=admin)
    receipt = convert_to_receipt(session, by=admin)
    assert [line.part_type for line in receipt.lines.all()] == [bronco]
    post_session(session, by=admin)
    assert PartType.objects.count() == parts_before
    balances = StockBalance.objects.filter(part_type=bronco, location=location)
    assert sum(b.quantity_physical for b in balances) == Decimal("2")
    assert StockMovement.objects.filter(part_type=bronco).count() == 1


def test_counting_page_shows_card_quantity_and_cell(client, bronco, location, admin):
    client.force_login(admin)
    session = start_session(location=location, by=admin)
    url = reverse("counting_scan", args=[session.pk])
    client.post(url, {"code": "127388\r\n"})
    client.post(url, {"code": "AT-04164"})
    page = client.get(reverse("counting_detail", args=[session.pk])).content.decode()
    assert escape(NAME) in page
    assert "Неизвестная деталь" not in page
    assert "1-3-8" in page
    line = session.lines.get()
    assert line.quantity_counted == Decimal("2")
    assert line.warehouse_part == bronco


# --- Scanner receiving ---------------------------------------------------------


@pytest.mark.parametrize("raw", ["AT-04164", "127388", "127388\n"])
def test_receiving_candidates_use_the_existing_card(bronco, raw):
    candidates = find_receiving_candidates(raw)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.source == "warehouse"
    assert candidate.part_id == bronco.pk
    assert candidate.exact_number == "AT-04164"
    assert candidate.manufacturer == "BRONCO PRODUCTS"


def test_receiving_ambiguous_reference_offers_a_choice(bronco, category, unit):
    other = _card("OTHER KIT", category, unit, article="ZZ-1", internal_ref="127388")
    candidates = find_receiving_candidates("127388")
    assert {c.part_id for c in candidates} == {bronco.pk, other.pk}


def test_receiving_scan_queues_the_existing_card_at_zero_stock(client, bronco, admin):
    client.force_login(admin)
    parts_before = PartType.objects.count()
    url = reverse("scanner_receiving")
    client.post(url, {"action": "scan", "code": "127388\n"})
    client.post(url, {"action": "scan", "code": "AT-04164"})
    queue = client.session[QUEUE_SESSION_KEY]
    lines = list(queue["lines"].values())
    assert len(lines) == 1
    assert lines[0]["source"] == "warehouse"
    assert lines[0]["source_id"] == bronco.pk
    assert lines[0]["exact_number"] == "AT-04164"
    assert Decimal(str(lines[0]["quantity"])) == Decimal("2")
    # Queueing never touches stock or the catalog.
    assert PartType.objects.count() == parts_before
    assert not StockBalance.objects.filter(part_type=bronco).exists()
    assert not StockMovement.objects.exists()


def test_receiving_scan_of_ambiguous_reference_asks_for_a_choice(
    client, bronco, admin, category, unit
):
    _card("OTHER KIT", category, unit, article="ZZ-1", internal_ref="127388")
    client.force_login(admin)
    resp = client.post(reverse("scanner_receiving"), {"action": "scan", "code": "127388"},
                       follow=True)
    assert "Выберите точную позицию" in resp.content.decode()
    assert not client.session.get(QUEUE_SESSION_KEY, {}).get("lines")
