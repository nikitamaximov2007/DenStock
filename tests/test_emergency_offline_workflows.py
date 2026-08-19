from decimal import Decimal

import pytest
from django.test import override_settings

from apps.actions.cart import KIND_SALE, add_scan, complete_cart, open_cart
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import move_stock_lot
from apps.operations.models import DeploymentState
from apps.operations.write_guard import BusinessWriteBlocked
from apps.receipts.services import add_line, create_receipt, post_receipt
from apps.sales.models import Reservation, Sale
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    complete_sale,
    create_reservation,
    create_sale_from_reservation,
)
from apps.stocktaking.models import SectionRecount
from apps.stocktaking.section_recount import (
    apply_section_recount,
    complete_section_cell,
    create_cell_recount,
    mark_section_ready,
    record_section_part,
    set_section_line_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local")
def test_core_warehouse_workflow_operates_on_active_offline_database(django_user_model):
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    user = django_user_model.objects.create_superuser(username="offline-warehouse", password="test")
    category = Category.objects.create(name="Offline category")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    part = PartType.objects.create(
        name="Offline bulk part",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("250"),
    )
    supplier = Supplier.objects.create(name="Offline supplier")
    source = StorageLocation.objects.create(
        code="S90-D01-C01", name="Offline source", storage_allowed=True
    )
    destination = StorageLocation.objects.create(
        code="S90-D01-C02", name="Offline destination", storage_allowed=True
    )

    receipt = create_receipt(supplier=supplier, by=user)
    add_line(
        receipt,
        part_type=part,
        quantity=Decimal("6"),
        unit_cost_rub=Decimal("100"),
        location=source,
    )
    receipt = post_receipt(receipt, by=user)
    lot = StockLot.objects.get(batch=receipt.batch, part_type=part)
    assert lot.quantity == Decimal("6")
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RECEIVE_LOT,
        stock_lot=lot,
    ).exists()

    move_stock_lot(lot, destination, by=user)
    lot.refresh_from_db()
    assert lot.location == destination
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.MOVE_LOT,
        stock_lot=lot,
    ).exists()

    reservation = create_reservation(
        customer_name="Offline client", customer_phone="+70000000000", by=user
    )
    add_stock_lot_to_reservation(reservation, lot, Decimal("1"), by=user)
    reservation = activate_reservation(reservation, by=user)
    assert reservation.status == Reservation.Status.ACTIVE
    sale = create_sale_from_reservation(reservation, by=user)
    sale = complete_sale(sale, by=user)
    lot.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED
    assert lot.quantity == Decimal("5")

    recount = create_cell_recount(location=destination, by=user)
    line = record_section_part(recount, cell_number=1, part_id=part.pk, by=user)
    set_section_line_quantity(line, Decimal("4"), by=user)
    complete_section_cell(recount, cell_number=1, by=user)
    recount = mark_section_ready(recount)
    recount = apply_section_recount(recount)
    lot.refresh_from_db()

    assert recount.status == SectionRecount.Status.COMPLETED
    assert lot.quantity == Decimal("4")
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.ADJUST_OUT,
        document_type="section_recount",
        document_id=recount.pk,
    ).exists()
    state.refresh_from_db()
    assert state.business_generation > 0


@pytest.mark.django_db
@override_settings(DENSTOCK_MODE="emergency-local")
def test_scanner_cart_completes_only_while_offline_session_is_active(django_user_model):
    state = DeploymentState.get_solo()
    state.write_state = DeploymentState.WriteState.EMERGENCY_ACTIVE
    state.save()
    user = django_user_model.objects.create_superuser(username="offline-cart", password="test")
    category = Category.objects.create(name="Offline cart category")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    part = PartType.objects.create(
        name="Offline cart part",
        category=category,
        unit=unit,
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("250"),
    )
    supplier = Supplier.objects.create(name="Offline cart supplier")
    location = StorageLocation.objects.create(
        code="S90-D02-C01", name="Offline cart location", storage_allowed=True
    )
    receipt = create_receipt(supplier=supplier, by=user)
    add_line(
        receipt,
        part_type=part,
        quantity=Decimal("2"),
        unit_cost_rub=Decimal("100"),
        location=location,
    )
    receipt = post_receipt(receipt, by=user)
    lot = StockLot.objects.get(batch=receipt.batch, part_type=part)

    active_cart = open_cart(KIND_SALE, by=user)
    add_scan(active_cart, part, location, by=user)
    complete_cart(active_cart, customer_comment="Offline customer", by=user)
    lot.refresh_from_db()
    assert lot.quantity == Decimal("1")

    frozen_cart = open_cart(KIND_SALE, by=user)
    add_scan(frozen_cart, part, location, by=user)
    state.write_state = DeploymentState.WriteState.EMERGENCY_FROZEN
    state.save()
    with pytest.raises(BusinessWriteBlocked):
        complete_cart(frozen_cart, customer_comment="Must stay frozen", by=user)
    lot.refresh_from_db()
    assert lot.quantity == Decimal("1")
