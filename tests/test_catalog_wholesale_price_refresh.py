"""Wholesale catalog pricing: current recommendations only, never stock history."""

from decimal import Decimal

import pytest
from django.core.management import call_command

from apps.actions.models import WarehouseAction
from apps.actions.services import perform_action
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse
from apps.inventory.models import PartPreferredLocation, StockBalance, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.models import SaleLine
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation


@pytest.fixture
def wholesale_price_data(db, django_user_model):
    user = django_user_model.objects.create_superuser("wholesale-admin", password="parol-12345")
    location = StorageLocation.objects.create(
        name="Ячейка пересчёта цен",
        code="S08-L01-D01-C01",
        storage_allowed=True,
        is_active=True,
    )
    brp = BrpCatalogPart.objects.create(
        material_no="WHOLESALE-PRICE-001",
        part_desc="WHOLESALE PRICE TEST",
        retail_price_usd=Decimal("100"),
        wholesale_price_usd=Decimal("10"),
    )
    part = promote_to_warehouse(brp, by=user)
    supplier = Supplier.objects.create(name="Поставщик пересчёта цен")
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch,
        part_type=part,
        quantity=Decimal("2"),
        unit_cost_currency=Decimal("1"),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, user)
    line.refresh_from_db()
    lot = create_stock_lot(line, location, Decimal("2"))
    receive_stock_lot(lot, by=user)
    preferred, _ = PartPreferredLocation.objects.get_or_create(
        part_type=part,
        defaults={"location": location, "updated_by": user},
    )
    action = perform_action(
        part=part,
        location=location,
        action_type=WarehouseAction.Type.SALE,
        quantity=Decimal("1"),
        customer_comment="Проверка снимка цены",
        by=user,
    )
    return {
        "action": action,
        "brp": brp,
        "lot": lot,
        "part": part,
        "preferred": preferred,
    }


def _stock_state(data):
    data["lot"].refresh_from_db()
    return {
        "balance_rows": StockBalance.objects.count(),
        "lot_quantity": data["lot"].quantity,
        "movements": StockMovement.objects.count(),
    }


def test_dry_run_and_apply_recalculate_only_current_recommendation(
    wholesale_price_data, capsys
):
    data = wholesale_price_data
    part = data["part"]
    link = part.brp_link
    action = data["action"]
    sale_line = SaleLine.objects.get(sale=action.sale)
    before_stock = _stock_state(data)

    assert part.recommended_price == Decimal("1470")
    assert link.calculated_customer_price_rub == Decimal("1470")
    assert action.unit_price_rub == Decimal("1470")
    assert sale_line.unit_price == Decimal("1470")

    data["brp"].wholesale_price_usd = Decimal("12")
    data["brp"].save(update_fields=["wholesale_price_usd"])

    call_command("recalculate_linked_part_prices")
    output = capsys.readouterr().out
    assert "Режим: DRY-RUN" in output
    assert "К изменению рекомендованных цен: 1" in output

    part.refresh_from_db()
    link.refresh_from_db()
    action.refresh_from_db()
    sale_line.refresh_from_db()
    data["preferred"].refresh_from_db()
    assert part.recommended_price == Decimal("1470")
    assert link.calculated_customer_price_rub == Decimal("1470")
    assert action.unit_price_rub == Decimal("1470")
    assert sale_line.unit_price == Decimal("1470")
    assert data["preferred"].location_id == action.location_id
    assert _stock_state(data) == before_stock

    call_command("recalculate_linked_part_prices", "--apply")
    output = capsys.readouterr().out
    assert "Режим: ПРИМЕНЕНИЕ" in output
    assert "Обновлено рекомендованных цен: 1" in output

    part.refresh_from_db()
    link.refresh_from_db()
    action.refresh_from_db()
    sale_line.refresh_from_db()
    data["preferred"].refresh_from_db()
    assert part.recommended_price == Decimal("1764")
    assert link.calculated_customer_price_rub == Decimal("1470")
    assert action.unit_price_rub == Decimal("1470")
    assert sale_line.unit_price == Decimal("1470")
    assert data["preferred"].location_id == action.location_id
    assert _stock_state(data) == before_stock

    call_command("recalculate_linked_part_prices")
    assert "К изменению рекомендованных цен: 0" in capsys.readouterr().out


def test_recalculate_keeps_existing_recommendation_when_wholesale_price_is_missing(
    db, django_user_model, capsys
):
    user = django_user_model.objects.create_superuser("missing-wholesale", password="parol-12345")
    brp = BrpCatalogPart.objects.create(
        material_no="WHOLESALE-PRICE-EMPTY",
        part_desc="MISSING WHOLESALE",
        retail_price_usd=Decimal("100"),
        wholesale_price_usd=None,
    )
    part = promote_to_warehouse(brp, by=user, manual_price=Decimal("4321"))
    part.brp_link.price_source = part.brp_link.PriceSource.CALCULATED
    part.brp_link.save(update_fields=["price_source"])

    call_command("recalculate_linked_part_prices", "--apply")

    part.refresh_from_db()
    assert part.recommended_price == Decimal("4321")
    assert "Без оптовой цены, текущая цена сохранена: 1" in capsys.readouterr().out
