from decimal import Decimal

import pytest

from apps.catalog.models import Category, Manufacturer, Unit
from apps.inventory.pricing import resolve_effective_inventory_customer_price
from apps.inventory.services import create_stock_lot


@pytest.fixture
def domain_env(db, django_user_model):
    from apps.suppliers.models import Supplier
    from apps.warehouse.models import StorageLocation

    user = django_user_model.objects.create_superuser(username="price-admin", password="pass")
    return {
        "user": user,
        "category": Category.objects.create(name="Receipt price tests"),
        "manufacturer": Manufacturer.objects.create(name="Receipt price maker"),
        "supplier": Supplier.objects.create(name="Receipt price supplier"),
        "unit": Unit.objects.get(name="Штука"),
        "loc1": StorageLocation.objects.create(
            name="Receipt price cell", code="S01-D01-C01", storage_allowed=True, is_active=True
        ),
    }


def _part(env, price):
    from apps.catalog.models import PartType

    return PartType.objects.create(
        name="Protected part", category=env["category"], manufacturer=env["manufacturer"],
        unit=env["unit"], tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal(price),
    )


def _line(env, part, quantity):
    from apps.procurement.models import Batch, BatchLine
    from apps.procurement.services import finalize_cost

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


def test_bulk_lot_keeps_customer_price_at_creation(domain_env):
    part = _part(domain_env, "22000")
    line = _line(domain_env, part, "2")
    lot = create_stock_lot(line, domain_env["loc1"], "1")
    assert lot.receipt_customer_price_rub == Decimal("22000")

    part.recommended_price = Decimal("18000")
    part.save(update_fields=["recommended_price"])
    lot.refresh_from_db()
    assert lot.receipt_customer_price_rub == Decimal("22000")
    assert resolve_effective_inventory_customer_price(
        lot, part.recommended_price
    ) == Decimal("22000")


def test_current_price_can_exceed_receipt_floor(domain_env):
    part = _part(domain_env, "22000")
    line = _line(domain_env, part, "1")
    lot = create_stock_lot(line, domain_env["loc1"], "1")
    part.recommended_price = Decimal("25000")
    part.save(update_fields=["recommended_price"])
    lot.refresh_from_db()
    assert resolve_effective_inventory_customer_price(
        lot, part.recommended_price
    ) == Decimal("25000")


def test_unknown_snapshot_falls_back_to_current_price(domain_env):
    part = _part(domain_env, "18000")
    line = _line(domain_env, part, "1")
    lot = create_stock_lot(line, domain_env["loc1"], "1")
    lot.receipt_customer_price_rub = None
    lot.save(update_fields=["receipt_customer_price_rub"])
    assert resolve_effective_inventory_customer_price(
        lot, part.recommended_price
    ) == Decimal("18000")
