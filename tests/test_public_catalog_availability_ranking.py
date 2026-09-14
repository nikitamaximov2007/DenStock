"""Availability-first ordering for the public, reservation-aware catalog."""

from decimal import Decimal

from apps.catalog.models import PartType
from apps.catalog.public_catalog import PAGE_SIZE, search_catalog
from apps.inventory.services import create_part_items, receive_part_item
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)


def _ids(result):
    return [card.facts.public_id for card in result.cards]


def _serial_item(public_catalog, part, *, number):
    batch = Batch.objects.create(supplier=public_catalog.supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal("1"),
        unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, public_catalog.user)
    line.refresh_from_db()
    item = create_part_items(line, serial_number=number)[0]
    receive_part_item(item, to_location=public_catalog.location, by=public_catalog.user)
    return item


def test_non_exact_results_put_canonical_available_quantity_before_name_relevance(public_catalog):
    exact_empty = public_catalog.part("CIRCLIP", article="C-EMPTY")
    exact_stocked = public_catalog.part("CIRCLIP", article="C-STOCK")
    prefix_stocked = public_catalog.part("CIRCLIP RETAINER", article="C-PREFIX")
    partial_empty = public_catalog.part("REAR CIRCLIP KIT", article="C-PARTIAL")
    public_catalog.stock(exact_stocked, "5")
    public_catalog.stock(prefix_stocked, "3")

    result = search_catalog("circlip", {})

    # Availability is decisive for all non-exact-article matches; the prior
    # Search 2.0 order remains intact inside each availability bucket.
    assert _ids(result) == [
        exact_stocked.public_id,
        prefix_stocked.public_id,
        exact_empty.public_id,
        partial_empty.public_id,
    ]


def test_exact_article_stays_above_an_available_name_match(public_catalog):
    exact_empty = public_catalog.part("UNRELATED EXACT", article="420-832-176")
    available_name = public_catalog.part("420-832-176 SERVICE KIT", article="SERVICE-1")
    public_catalog.stock(available_name, "1")

    assert _ids(search_catalog("420-832-176", {}))[0] == exact_empty.public_id
    assert _ids(search_catalog("420 832 176", {}))[0] == exact_empty.public_id


def test_exact_article_duplicates_use_availability_only_inside_the_protected_tier(public_catalog):
    empty = public_catalog.part("EXACT EMPTY", article="420-832-176")
    stocked = public_catalog.part("EXACT STOCKED", article="420-832-176")
    available_name = public_catalog.part("420-832-176 SERVICE KIT", article="SERVICE-2")
    public_catalog.stock(stocked, "1")
    public_catalog.stock(available_name, "9")

    assert _ids(search_catalog("420-832-176", {})) == [
        stocked.public_id,
        empty.public_id,
        available_name.public_id,
    ]


def test_partial_article_uses_availability_first_not_the_exact_exception(public_catalog):
    empty_prefix = public_catalog.part("PREFIX EMPTY", article="420-832-176-A")
    stocked_prefix = public_catalog.part("PREFIX STOCKED", article="420-832-176-B")
    public_catalog.stock(stocked_prefix, "1")

    assert _ids(search_catalog("420-832", {})) == [stocked_prefix.public_id, empty_prefix.public_id]


def test_availability_is_ranked_before_pagination_and_order_is_stable(public_catalog):
    empty = [
        public_catalog.part(f"PAGINATION CIRCLIP {index:02d}", article=f"PAGE-{index:02d}")
        for index in range(PAGE_SIZE)
    ]
    stocked = public_catalog.part("PAGINATION CIRCLIP STOCKED", article="PAGE-STOCK")
    public_catalog.stock(stocked, "1")

    first = search_catalog("pagination circlip", {})
    second = search_catalog("pagination circlip", {"page": "2"})

    assert first.cards[0].facts.public_id == stocked.public_id
    assert {card.facts.public_id for card in first.cards[1:]} == {
        part.public_id for part in empty[:19]
    }
    assert _ids(second) == [empty[-1].public_id]
    assert _ids(search_catalog("pagination circlip", {})) == _ids(first)


def test_reservations_drive_the_availability_bucket_not_physical_quantity(public_catalog):
    reserved = public_catalog.part("RESERVATION CIRCLIP A", article="RES-A")
    free = public_catalog.part("RESERVATION CIRCLIP B", article="RES-B")
    lot = public_catalog.stock(reserved, "2")
    public_catalog.stock(free, "2")
    reservation = create_reservation(customer_name="Ranking test", by=public_catalog.user)
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=public_catalog.user)
    activate_reservation(reservation, by=public_catalog.user)

    result = search_catalog("reservation circlip", {})
    assert _ids(result) == [free.public_id, reserved.public_id]
    assert result.cards[0].facts.available_quantity == Decimal("2")
    assert result.cards[1].facts.available_quantity == Decimal("0")


def test_serial_and_multiple_lot_availability_are_aggregated_canonically(public_catalog):
    serial = public_catalog.part("SERIAL CIRCLIP", article="SERIAL-C")
    serial.tracking_mode = PartType.TrackingMode.SERIAL
    serial.save(update_fields=["tracking_mode"])
    _serial_item(public_catalog, serial, number="RANKING-SERIAL-1")

    multi_lot = public_catalog.part("MULTI LOT CIRCLIP", article="MULTI-C")
    public_catalog.stock(multi_lot, "3")
    empty = public_catalog.part("EMPTY CIRCLIP", article="EMPTY-C")

    result = search_catalog("circlip", {})
    by_id = {card.facts.public_id: card.facts.available_quantity for card in result.cards}
    assert by_id[serial.public_id] == Decimal("1")
    assert by_id[multi_lot.public_id] == Decimal("3")
    assert by_id[empty.public_id] == Decimal("0")
    assert _ids(result).index(empty.public_id) > _ids(result).index(serial.public_id)
    assert _ids(result).index(empty.public_id) > _ids(result).index(multi_lot.public_id)


def test_price_state_does_not_affect_availability_order_or_in_stock_filter(public_catalog):
    clarify_stocked = public_catalog.part("PRICE CIRCLIP A", article="PRICE-A", price=None)
    priced_empty = public_catalog.part("PRICE CIRCLIP B", article="PRICE-B", price="500")
    public_catalog.stock(clarify_stocked, "1")

    result = search_catalog("price circlip", {})
    assert _ids(result) == [clarify_stocked.public_id, priced_empty.public_id]
    assert _ids(search_catalog("price circlip", {"in_stock": "1"})) == [clarify_stocked.public_id]
