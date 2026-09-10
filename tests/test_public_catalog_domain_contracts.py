"""Stage 1 contracts for the future Pro-Stor Public Catalog.

These tests keep the public read facade aligned with the existing pricing and
inventory truth without adding any public routes or publication state.
"""

from dataclasses import fields
from datetime import timedelta
from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.actions.models import PartCustomsInfo
from apps.brp.models import BrpCatalogPart, BrpPricingSettings
from apps.brp.services import promote_to_warehouse
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.catalog.public_contracts import (
    ZERO,
    CurrentCustomerPrice,
    PublicPartFacts,
    PublicUnit,
    build_public_part_facts,
    resolve_current_customer_price,
)
from apps.catalog.services import update_current_price_settings
from apps.inventory.availability import available_totals
from apps.inventory.models import PartItem, StockBalance, StockLot
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import Reservation, ReservationLine
from apps.sales.services import (
    activate_reservation,
    add_part_item_to_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation, ValuationSettings
from tests.customs_support import remember_customs


@pytest.fixture
def domain_env(db, django_user_model):
    user = django_user_model.objects.create_superuser(
        username="public-domain-admin", password="parol-12345"
    )
    return {
        "user": user,
        "category": Category.objects.create(name="Public domain tests"),
        "manufacturer": Manufacturer.objects.create(name="Canonical manufacturer"),
        "supplier": Supplier.objects.create(name="Public domain supplier"),
        "unit": Unit.objects.get(name="Штука"),
        "loc1": StorageLocation.objects.create(
            name="Public cell one", code="S01-D01-C01", storage_allowed=True, is_active=True
        ),
        "loc2": StorageLocation.objects.create(
            name="Public cell two", code="S01-D01-C02", storage_allowed=True, is_active=True
        ),
    }


def _part(env, *, name="English part", tracking=PartType.TrackingMode.BULK, price="100"):
    return PartType.objects.create(
        name=name,
        category=env["category"],
        manufacturer=env["manufacturer"],
        unit=env["unit"],
        tracking_mode=tracking,
        recommended_price=Decimal(price) if price is not None else None,
    )


def _finalized_line(env, part, *, quantity):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal(quantity),
        unit_cost_currency=Decimal("10"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["user"])
    line.refresh_from_db()
    return line


def _bulk_lot(env, part, quantity, *, location=None, receive=True):
    line = _finalized_line(env, part, quantity=quantity)
    lot = create_stock_lot(line, location or env["loc1"], Decimal(quantity))
    if receive:
        receive_stock_lot(lot, by=env["user"])
    return lot


def _serial_item(env, part, *, location=None, receive=True, serial_number="SERIAL-1"):
    line = _finalized_line(env, part, quantity="1")
    item = create_part_items(line, serial_number=serial_number)[0]
    if receive:
        receive_part_item(item, to_location=location or env["loc1"], by=env["user"])
    return item


def _assert_read_only(queries):
    write_prefixes = ("INSERT", "UPDATE", "DELETE", "REPLACE", "ALTER", "CREATE", "DROP")
    assert all(
        not query["sql"].lstrip().upper().startswith(write_prefixes)
        for query in queries.captured_queries
    )


# --- Price facade ---------------------------------------------------------------


def test_price_facade_returns_known_decimal_without_mutating_state(domain_env):
    part = _part(domain_env, price="1234.50")
    before = (
        PartType.objects.get(pk=part.pk).recommended_price,
        ValuationSettings.objects.count(),
        BrpPricingSettings.objects.count(),
    )

    with CaptureQueriesContext(connection) as queries:
        result = resolve_current_customer_price(part)

    assert result == CurrentCustomerPrice(price_rub=Decimal("1234.50"), status="known")
    assert isinstance(result.price_rub, Decimal)
    assert not queries.captured_queries
    assert (
        PartType.objects.get(pk=part.pk).recommended_price,
        ValuationSettings.objects.count(),
        BrpPricingSettings.objects.count(),
    ) == before


@pytest.mark.parametrize("value", [None, "0", "-1"])
def test_price_facade_returns_clarify_for_missing_or_unusable_price(domain_env, value):
    part = _part(domain_env, price=value)

    assert resolve_current_customer_price(part) == CurrentCustomerPrice(
        price_rub=None,
        status="clarify",
    )


def test_price_facade_sees_existing_canonical_price_refresh_immediately(domain_env):
    catalog_part = BrpCatalogPart.objects.create(
        material_no="PUBLIC-PRICE-001",
        part_desc="Public price source",
        retail_price_usd=Decimal("100"),
        wholesale_price_usd=Decimal("10"),
    )
    part = promote_to_warehouse(catalog_part, by=domain_env["user"])

    assert resolve_current_customer_price(part).price_rub == Decimal("1470")

    update_current_price_settings(
        current_usd_rate=Decimal("100"),
        brp_markup_percent=Decimal("50"),
        polaris_markup_percent=Decimal("40"),
        by=domain_env["user"],
    )
    part.refresh_from_db()

    assert resolve_current_customer_price(part) == CurrentCustomerPrice(
        price_rub=Decimal("1500"),
        status="known",
    )


# --- Available totals -----------------------------------------------------------


def test_available_totals_for_bulk_lots_and_multiple_cells(domain_env):
    part = _part(domain_env)
    _bulk_lot(domain_env, part, "2")
    _bulk_lot(domain_env, part, "3", location=domain_env["loc2"])

    result = available_totals([part.pk])

    assert result == {part.pk: Decimal("5")}
    assert all(isinstance(value, Decimal) for value in result.values())
    assert not any(hasattr(result, attribute) for attribute in ("location", "lot", "batch"))


def test_available_totals_uses_only_active_unexpired_reservations(domain_env):
    part = _part(domain_env)
    active_lot = _bulk_lot(domain_env, part, "5")
    expired_lot = _bulk_lot(domain_env, part, "4", location=domain_env["loc2"])

    active = create_reservation(customer_name="Active", by=domain_env["user"])
    add_stock_lot_to_reservation(active, active_lot, Decimal("2"), by=domain_env["user"])
    activate_reservation(active, by=domain_env["user"])

    expired = Reservation.objects.create(
        customer_name="Expired",
        status=Reservation.Status.ACTIVE,
        expires_at=timezone.now() - timedelta(minutes=1),
        created_by=domain_env["user"],
    )
    ReservationLine.objects.create(
        reservation=expired,
        part_type=part,
        stock_lot=expired_lot,
        quantity=Decimal("4"),
    )
    draft = Reservation.objects.create(
        customer_name="Draft",
        status=Reservation.Status.DRAFT,
        created_by=domain_env["user"],
    )
    ReservationLine.objects.create(
        reservation=draft,
        part_type=part,
        stock_lot=expired_lot,
        quantity=Decimal("1"),
    )

    assert available_totals([part.pk]) == {part.pk: Decimal("7")}


def test_available_totals_excludes_fully_reserved_receiving_and_quarantine_bulk(domain_env):
    part = _part(domain_env)
    reserved_lot = _bulk_lot(domain_env, part, "5")
    receiving_lot = _bulk_lot(domain_env, part, "4", location=domain_env["loc2"], receive=False)
    quarantine_lot = _bulk_lot(domain_env, part, "3", location=domain_env["loc2"])
    StockLot.objects.filter(pk=quarantine_lot.pk).update(status=StockLot.Status.QUARANTINE)

    reservation = create_reservation(customer_name="All reserved", by=domain_env["user"])
    add_stock_lot_to_reservation(
        reservation,
        reserved_lot,
        Decimal("5"),
        by=domain_env["user"],
    )
    activate_reservation(reservation, by=domain_env["user"])

    assert receiving_lot.status == StockLot.Status.RECEIVING
    assert available_totals([part.pk]) == {part.pk: Decimal("0")}


def test_available_totals_counts_serial_and_excludes_reserved_serial(domain_env):
    part = _part(domain_env, tracking=PartType.TrackingMode.SERIAL)
    available = _serial_item(domain_env, part, serial_number="PUBLIC-SERIAL-1")
    reserved = _serial_item(domain_env, part, serial_number="PUBLIC-SERIAL-2")
    _serial_item(
        domain_env,
        part,
        location=domain_env["loc2"],
        receive=False,
        serial_number="PUBLIC-SERIAL-3",
    )
    quarantined = _serial_item(
        domain_env,
        part,
        location=domain_env["loc2"],
        serial_number="PUBLIC-SERIAL-4",
    )
    PartItem.objects.filter(pk=quarantined.pk).update(status=PartItem.Status.QUARANTINE)

    reservation = create_reservation(customer_name="Serial reserve", by=domain_env["user"])
    add_part_item_to_reservation(reservation, reserved, by=domain_env["user"])
    activate_reservation(reservation, by=domain_env["user"])

    assert available.pk != reserved.pk
    assert available_totals([part.pk]) == {part.pk: Decimal("1")}


def test_available_totals_is_batched_zero_filled_and_ignores_stockbalance_cache(domain_env):
    stocked = _part(domain_env, name="Stocked")
    empty = _part(domain_env, name="Empty")
    lot = _bulk_lot(domain_env, stocked, "5")
    StockBalance.objects.filter(batch_line=lot.batch_line, location=lot.location).update(
        quantity_available=Decimal("999")
    )

    assert available_totals([stocked.pk, empty.pk]) == {
        stocked.pk: Decimal("5"),
        empty.pk: Decimal("0"),
    }


def test_available_totals_reflects_next_sale_read_without_cache_invalidation(domain_env):
    from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale

    part = _part(domain_env)
    lot = _bulk_lot(domain_env, part, "5")
    remember_customs(part)

    assert available_totals([part.pk])[part.pk] == Decimal("5")

    sale = create_sale(customer_name="Public catalog buyer", by=domain_env["user"])
    add_stock_lot_to_sale(sale, lot, Decimal("2"), unit_price=Decimal("100"), by=domain_env["user"])
    complete_sale(sale, by=domain_env["user"])

    assert available_totals([part.pk])[part.pk] == Decimal("3")


def test_available_totals_is_read_only(domain_env):
    part = _part(domain_env)
    _bulk_lot(domain_env, part, "1")

    with CaptureQueriesContext(connection) as queries:
        assert available_totals([part.pk]) == {part.pk: Decimal("1")}

    _assert_read_only(queries)


# --- Public facts ---------------------------------------------------------------


def test_public_part_facts_use_canonical_identity_confirmed_ru_name_and_unit(domain_env):
    meter = Unit.objects.get(name="Метр")
    part = PartType.objects.create(
        name="Fuel hose",
        category=domain_env["category"],
        manufacturer=domain_env["manufacturer"],
        unit=meter,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("400.25"),
    )
    PartNumber.objects.create(
        part=part,
        value="HOSE-001",
        kind=PartNumber.Kind.ARTICLE,
        is_primary=True,
    )
    _bulk_lot(domain_env, part, "2")
    PartCustomsInfo.objects.create(
        part_type=part,
        customs_name_ru="Топливный шланг",
        customs_name_ru_confirmed=True,
    )
    unconfirmed = _part(domain_env, name="Unconfirmed name")
    PartCustomsInfo.objects.create(
        part_type=unconfirmed,
        customs_name_ru="Неподтверждённое название",
        customs_name_ru_confirmed=False,
    )

    facts, hidden = build_public_part_facts([part.pk, unconfirmed.pk])

    assert facts == PublicPartFacts(
        part_id=part.pk,
        article="HOSE-001",
        english_name="Fuel hose",
        russian_name="Топливный шланг",
        manufacturer="Canonical manufacturer",
        unit=PublicUnit(name="Метр", short_name="м"),
        price=CurrentCustomerPrice(price_rub=Decimal("400.25"), status="known"),
        available_quantity=Decimal("2"),
    )
    assert hidden.russian_name is None
    assert hidden.available_quantity == Decimal("0")


def test_public_part_facts_preserve_canonical_catalog_identity_over_card_metadata(domain_env):
    catalog_part = BrpCatalogPart.objects.create(
        material_no="PUBLIC-IDENTITY-001",
        part_desc="Catalog identity",
        wholesale_price_usd=Decimal("10"),
    )
    part = promote_to_warehouse(catalog_part, by=domain_env["user"])

    facts = build_public_part_facts([part.pk])[0]

    assert facts.article == "PUBLIC-IDENTITY-001"
    assert facts.manufacturer == "BRP"


def test_public_part_facts_expose_no_internal_stock_or_commercial_fields(domain_env):
    part = _part(domain_env)
    facts = build_public_part_facts([part.pk])[0]

    assert {field.name for field in fields(PublicPartFacts)} == {
        "part_id",
        "article",
        "english_name",
        "russian_name",
        "manufacturer",
        "unit",
        "price",
        "available_quantity",
    }
    for forbidden in (
        "location",
        "lot",
        "batch",
        "serial",
        "barcode",
        "receipt",
        "supplier",
        "purchase_cost",
        "min_price",
        "markup",
        "fx",
        "internal_note",
        "customer",
        "staff",
        "movement",
    ):
        assert not hasattr(facts, forbidden)


def test_public_part_facts_are_read_only_and_have_bounded_query_count(domain_env):
    parts = []
    for index in range(50):
        part = _part(domain_env, name=f"Bounded public part {index}")
        PartNumber.objects.create(
            part=part,
            value=f"PUBLIC-{index:03d}",
            kind=PartNumber.Kind.ARTICLE,
            is_primary=True,
        )
        parts.append(part)

    query_counts = []
    for count in (1, 20, 50):
        with CaptureQueriesContext(connection) as queries:
            facts = build_public_part_facts([part.pk for part in parts[:count]])
        assert len(facts) == count
        _assert_read_only(queries)
        query_counts.append(len(queries))

    assert query_counts[1] <= query_counts[0] + 1, query_counts
    assert query_counts[2] <= query_counts[0] + 1, query_counts



def test_public_part_facts_query_count_is_constant_with_real_stock(domain_env):
    """Stage 1 hardening: the bounded-query guard must cover the stock branch.

    The guard above builds parts WITHOUT stock, so ``live_stock_rows`` returns
    early and never runs its reservation lookup or identity hydration. Real
    public traffic is mostly parts WITH stock, which is exactly where any
    future per-row work would appear.

    Every batch here holds the same branch mix - a serial part, a partly
    reserved bulk part, and plain bulk parts - so the only thing that changes
    between batches is size. A serial part adds one fixed lookup (reserved
    serial items); that is a branch, not per-part growth, so the mix is kept
    identical rather than letting the largest batch be the only one with it.
    """
    serial_part = _part(
        domain_env, name="Stocked serial part", tracking=PartType.TrackingMode.SERIAL
    )
    _serial_item(domain_env, serial_part, serial_number="HARDEN-1")
    parts = [serial_part]
    for index in range(49):
        part = _part(domain_env, name=f"Stocked public part {index}")
        PartNumber.objects.create(
            part=part, value=f"STOCKED-{index:03d}",
            kind=PartNumber.Kind.ARTICLE, is_primary=True,
        )
        _bulk_lot(domain_env, part, "3")
        parts.append(part)
    reserved = create_reservation(customer_name="Hardening", by=domain_env["user"])
    add_stock_lot_to_reservation(
        reserved, StockLot.objects.filter(part_type=parts[1]).get(), Decimal("1"),
        by=domain_env["user"],
    )
    activate_reservation(reserved, by=domain_env["user"])

    query_counts = []
    for count in (2, 20, 50):
        with CaptureQueriesContext(connection) as queries:
            facts = build_public_part_facts([part.pk for part in parts[:count]])
        assert len(facts) == count
        assert all(fact.available_quantity >= ZERO for fact in facts)
        _assert_read_only(queries)
        query_counts.append(len(queries))

    # Constant with stock: no per-part query appears as the batch grows.
    assert query_counts[0] == query_counts[1] == query_counts[2], query_counts
    # The stock branch really ran: reservations and identity hydration add
    # queries on top of the five-query no-stock baseline.
    assert query_counts[0] > 5, query_counts
