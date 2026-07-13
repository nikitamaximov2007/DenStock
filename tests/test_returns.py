"""Слой 18 — возвраты на склад (физическое обратное поступление).

Покрывает план 18-layer-18-stock-returns.md §22. Ключевое: возврат восстанавливает
физический остаток (StockMovement RETURN_*, статус/количество растут) и фиксирует
себестоимость из источника — но НЕ делает денежный refund и НЕ меняет
Sale/RepairOrder. Физику делают inventory.return_*, документ ведёт apps/returns;
view ledger напрямую не пишет.
"""
import inspect
import re
import urllib.parse
import urllib.request
from decimal import Decimal
from http.cookiejar import CookieJar
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.migrations.executor import MigrationExecutor
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from apps.accounts import roles
from apps.actions.models import WarehouseAction
from apps.actions.services import actions_report
from apps.brp.models import BrpCatalogPart
from apps.brp.services import promote_to_warehouse as promote_brp_to_warehouse
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import PartItem, StockBalance, StockLot, StockMovement
from apps.inventory.services import (
    InventoryError,
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.polaris.models import PolarisCatalogPart
from apps.polaris.services import promote_to_warehouse as promote_polaris_to_warehouse
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.repairs.services import (
    add_part_item_to_repair_order,
    add_stock_lot_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import (
    ReturnError,
    _locked_return_line_ids,
    _locked_source,
    _return_line_locking_queryset,
    add_repair_line_return,
    add_sale_line_return,
    complete_return,
    create_return,
    remove_return_line,
    returnable_quantity,
    update_return_line_restock_status,
)
from apps.sales.models import Sale
from apps.sales.services import (
    add_part_item_to_sale,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

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


def _finalized_line(sup, part, admin, *, qty, unit_cost="100", shipping="40"):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal(shipping))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(qty), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    loc2 = StorageLocation.objects.create(
        name="Ячейка B", code="B-01", storage_allowed=True, is_active=True
    )
    bad_loc = StorageLocation.objects.create(
        name="Зона приёмки", code="RECV", storage_allowed=False, is_active=True
    )
    inactive_loc = StorageLocation.objects.create(
        name="Архив", code="ARCH", storage_allowed=True, is_active=False
    )

    serial = PartType.objects.create(
        name="Насос-Возврат", category=cat, unit=unit,
        tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("400"),
    )
    iline = _finalized_line(sup, serial, admin, qty="2")  # landed_unit 120
    item_a = create_part_items(iline, 1, serial_number="SN-RET-A")[0]
    receive_part_item(item_a, to_location=loc, by=admin)
    item_b = create_part_items(iline, 1, serial_number="SN-RET-B")[0]
    receive_part_item(item_b, to_location=loc, by=admin)

    bulk = PartType.objects.create(
        name="Болт-Возврат", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    bline = _finalized_line(sup, bulk, admin, qty="10")  # landed_unit 104
    lot = create_stock_lot(bline, loc, Decimal("10"))
    receive_stock_lot(lot, by=admin)

    small = PartType.objects.create(
        name="Шайба-Возврат", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
    )
    sline = _finalized_line(sup, small, admin, qty="4")
    lot_small = create_stock_lot(sline, loc, Decimal("2"))
    receive_stock_lot(lot_small, by=admin)

    # Продажа: экземпляр item_a (1), 3 из lot, весь lot_small (2 → depleted).
    sale = create_sale(customer_name="Покупатель", by=admin)
    add_part_item_to_sale(sale, item_a, unit_price=Decimal("500"), by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("3"), unit_price=Decimal("200"), by=admin)
    add_stock_lot_to_sale(sale, lot_small, Decimal("2"), unit_price=Decimal("50"), by=admin)
    complete_sale(sale, by=admin)
    sale.refresh_from_db()

    # Ремонт: экземпляр item_b + 2 из lot.
    order = create_repair_order(customer_name="Клиент", by=admin)
    add_part_item_to_repair_order(order, item_b, by=admin)
    add_stock_lot_to_repair_order(order, lot, Decimal("2"), by=admin)
    complete_repair_order(order, by=admin)
    order.refresh_from_db()

    return {
        "admin": admin, "sup": sup, "loc": loc, "loc2": loc2, "bad_loc": bad_loc,
        "inactive_loc": inactive_loc, "item_a": item_a, "item_b": item_b,
        "lot": lot, "lot_small": lot_small, "sale": sale, "order": order,
        "sale_item_line": sale.lines.get(part_item=item_a),
        "sale_lot_line": sale.lines.get(stock_lot=lot),
        "sale_small_line": sale.lines.get(stock_lot=lot_small),
        "repair_item_line": order.lines.get(part_item=item_b),
        "repair_lot_line": order.lines.get(stock_lot=lot),
    }


def _new_return(data, source):
    return create_return(source=source, by=data["admin"])


# --- Создание / проведение ----------------------------------------------------


def test_create_draft_return(data):
    ret = _new_return(data, data["sale"])
    assert ret.status == StockReturn.Status.DRAFT
    assert ret.number.startswith("RET-")
    assert ret.source_type == StockReturn.SourceType.SALE


def test_cannot_complete_empty_return(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        complete_return(ret, by=data["admin"])
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT


def test_cannot_return_from_draft_sale(data):
    draft_sale = create_sale(customer_name="Черновик", by=data["admin"])
    with pytest.raises(ReturnError):
        create_return(source=draft_sale, by=data["admin"])


# --- Возврат проданного PartItem ---------------------------------------------


def test_return_sold_part_item_quarantine(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["item_a"].refresh_from_db()
    assert data["item_a"].status == PartItem.Status.QUARANTINE
    assert data["item_a"].current_location_id == data["loc"].pk
    mv = StockMovement.objects.get(
        movement_type=StockMovement.MovementType.RETURN_ITEM, part_item=data["item_a"]
    )
    assert mv.from_location_id is None
    assert mv.to_location_id == data["loc"].pk
    assert mv.quantity == Decimal("1")
    assert mv.document_type == "stock_return"
    assert mv.document_id == ret.pk


def test_return_sold_part_item_available_explicit(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["item_a"].refresh_from_db()
    assert data["item_a"].status == PartItem.Status.AVAILABLE
    bal = StockBalance.objects.get(batch_line=data["item_a"].batch_line, location=data["loc"])
    assert bal.quantity_available >= Decimal("1")


def test_return_increases_balance(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    bal = StockBalance.objects.get(batch_line=data["item_a"].batch_line, location=data["loc"])
    assert bal.quantity_physical == Decimal("1")
    assert bal.quantity_quarantine == Decimal("1")
    assert bal.quantity_available == Decimal("0")  # карантин не в доступном


# --- Возврат выданного в ремонт PartItem -------------------------------------


def test_return_installed_part_item(data):
    ret = _new_return(data, data["order"])
    assert ret.source_type == StockReturn.SourceType.REPAIR_ORDER
    line = add_repair_line_return(
        ret, data["repair_item_line"], Decimal("1"),
        to_location=data["loc2"], restock_status="quarantine", by=data["admin"],
    )
    assert line.to_location_id == data["loc"].pk
    complete_return(ret, by=data["admin"])
    data["item_b"].refresh_from_db()
    assert data["item_b"].status == PartItem.Status.QUARANTINE
    assert data["item_b"].current_location_id == data["loc"].pk
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_ITEM, part_item=data["item_b"]
    ).exists()


# --- Возврат StockLot quantity -----------------------------------------------


def test_return_sold_stock_lot_quantity(data):
    data["lot"].refresh_from_db()
    before = data["lot"].quantity  # 5 (10 − 3 продажа − 2 ремонт)
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == before + Decimal("2")
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT, stock_lot=data["lot"]
    ).exists()


def test_return_repair_stock_lot_quantity(data):
    data["lot"].refresh_from_db()
    before = data["lot"].quantity
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret, data["repair_lot_line"], Decimal("1"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == before + Decimal("1")


def test_repair_return_restores_exact_lot_location_and_action(data):
    part = data["lot"].part_type
    PartNumber.objects.create(
        part=part, value="420931284", kind=PartNumber.Kind.OEM, is_primary=True
    )
    PartNumber.objects.create(part=part, value="DS-LOT-ONLY", kind=PartNumber.Kind.INTERNAL_REF)
    data["lot"].refresh_from_db()
    before_source = data["lot"].quantity
    cost_before = data["order"].cost_total
    before_other = StockLot.objects.filter(location=data["loc2"]).count()
    ret = _new_return(data, data["order"])

    line = add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc2"],
        restock_status="available",
        by=data["admin"],
    )
    line.refresh_from_db()
    assert line.to_location_id == data["loc"].pk
    assert line.restock_status == "available"
    assert data["lot"].quantity == before_source
    assert WarehouseAction.objects.filter(action_type="repair_return").count() == 0

    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == before_source + Decimal("1")
    assert StockLot.objects.filter(location=data["loc2"]).count() == before_other
    line.refresh_from_db()
    assert line.returned_lot_id == data["lot"].pk
    action = WarehouseAction.objects.get(stock_return=ret)
    assert action.action_type == WarehouseAction.Type.REPAIR_RETURN
    assert action.quantity == Decimal("1")
    assert action.location_id == data["loc"].pk
    assert action.location_code == data["loc"].code
    assert action.stock_lot_id == data["lot"].pk
    assert action.repair_order_id == data["order"].pk
    assert action.repair_issue_line_id == data["repair_lot_line"].pk
    assert action.part_number == "420931284"
    assert action.part_number != "DS-LOT-ONLY"
    assert action.total_cost_rub == line.total_cost_rub
    data["order"].refresh_from_db()
    assert data["order"].cost_total == cost_before - line.total_cost_rub


def test_repair_return_draft_status_can_change_before_completion(data, client):
    data["lot"].refresh_from_db()
    balance = StockBalance.objects.get(batch_line=data["lot"].batch_line, location=data["loc"])
    lot_before = data["lot"].quantity
    balance_before = balance.quantity_physical
    movement_count = StockMovement.objects.count()
    ret = _new_return(data, data["order"])
    line = add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.QUARANTINE,
        by=data["admin"],
    )

    client.force_login(data["admin"])
    response = client.post(
        reverse("return_update_line_status", args=[line.pk]),
        {"restock_status": StockReturnLine.RestockStatus.AVAILABLE},
    )
    assert response.status_code == 302
    line.refresh_from_db()
    data["lot"].refresh_from_db()
    balance.refresh_from_db()
    assert line.restock_status == StockReturnLine.RestockStatus.AVAILABLE
    assert data["lot"].quantity == lot_before
    assert balance.quantity_physical == balance_before
    assert StockMovement.objects.count() == movement_count

    complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    assert data["lot"].quantity == lot_before + Decimal("1")
    assert data["lot"].status == StockLot.Status.AVAILABLE
    with pytest.raises(ReturnError):
        update_return_line_restock_status(
            line, restock_status=StockReturnLine.RestockStatus.QUARANTINE, by=data["admin"]
        )
    line.refresh_from_db()
    assert line.restock_status == StockReturnLine.RestockStatus.AVAILABLE


def test_repair_return_locking_query_loads_nullable_relations_separately(data):
    ret = _new_return(data, data["order"])
    line = add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    line.refresh_from_db()

    assert line.source_sale_line_id is None
    assert line.part_item_id is None
    assert line.returned_lot_id is None
    assert line.stock_lot_id == data["lot"].pk
    assert line.source_repair_line_id == data["repair_lot_line"].pk
    assert _locked_return_line_ids(ret) == [line.pk]
    assert "select_related" not in inspect.getsource(_locked_return_line_ids)
    assert "select_for_update().select_related" not in inspect.getsource(_locked_source)

    complete_return(ret, by=data["admin"])

    ret.refresh_from_db()
    line.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED
    assert line.returned_lot_id == data["lot"].pk
    assert (
        StockMovement.objects.filter(document_id=ret.pk, document_type="stock_return").count()
        == 1
    )
    assert WarehouseAction.objects.filter(stock_return=ret).count() == 1


def test_return_line_lock_query_generates_join_free_postgresql_sql(data):
    ret = _new_return(data, data["order"])
    postgres = DatabaseWrapper(
        {
            "NAME": "scratch",
            "USER": "",
            "PASSWORD": "",
            "HOST": "",
            "PORT": "",
            "OPTIONS": {},
            "TIME_ZONE": None,
            "CONN_HEALTH_CHECKS": False,
            "CONN_MAX_AGE": 0,
            "ATOMIC_REQUESTS": False,
            "AUTOCOMMIT": True,
        },
        "postgresql_sql_generation",
    )
    postgres.get_autocommit = lambda: False

    sql, _params = _return_line_locking_queryset(ret).query.get_compiler(
        connection=postgres
    ).as_sql()

    assert 'FROM "returns_stockreturnline"' in sql
    assert "FOR UPDATE" in sql
    assert "LEFT OUTER JOIN" not in sql


def test_legacy_repair_return_line_recovers_missing_source_link(data):
    ret = _new_return(data, data["order"])
    line = add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    line.source_repair_line_id = None  # Simulates a pre-0003 legacy draft row in memory.

    source = _locked_source(ret, line)

    assert source.pk == data["repair_lot_line"].pk


def test_return_complete_second_post_is_a_safe_redirect(data, client):
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    client.force_login(data["admin"])

    first = client.post(reverse("return_complete", args=[ret.pk]))
    action_count = WarehouseAction.objects.filter(stock_return=ret).count()
    movement_count = StockMovement.objects.count()
    second = client.post(reverse("return_complete", args=[ret.pk]))

    assert first.status_code == 302
    assert second.status_code == 302
    assert WarehouseAction.objects.filter(stock_return=ret).count() == action_count
    assert StockMovement.objects.count() == movement_count


def test_return_complete_maps_inventory_error_to_redirect_without_changes(data, client):
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    data["lot"].refresh_from_db()
    lot_before = data["lot"].quantity
    client.force_login(data["admin"])

    with patch(
        "apps.returns.services.return_stock_lot_quantity",
        side_effect=InventoryError("Конфликт остатка"),
    ):
        response = client.post(reverse("return_complete", args=[ret.pk]), follow=True)

    ret.refresh_from_db()
    data["lot"].refresh_from_db()
    assert response.status_code == 200
    assert "Конфликт остатка" in response.content.decode()
    assert ret.status == StockReturn.Status.DRAFT
    assert data["lot"].quantity == lot_before


@pytest.mark.parametrize(
    "failure_path",
    (
        "apps.inventory.services._record_movement",
        "apps.returns.services.WarehouseAction.objects.create",
        "apps.repairs.services.calculate_repair_costs",
    ),
)
def test_return_completion_rolls_back_after_critical_failure(data, failure_path):
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    data["lot"].refresh_from_db()
    lot_before = data["lot"].quantity
    movement_count = StockMovement.objects.count()

    with patch(failure_path, side_effect=RuntimeError):
        with pytest.raises(RuntimeError):
            complete_return(ret, by=data["admin"])

    ret.refresh_from_db()
    data["lot"].refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT
    assert data["lot"].quantity == lot_before
    assert StockMovement.objects.count() == movement_count


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_repair_return_migrations_preserve_legacy_quarantine_draft(data):
    """A 0002 draft remains operable after applying returns 0003/actions 0007."""
    ret = _new_return(data, data["order"])
    line = add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.QUARANTINE,
        by=data["admin"],
    )
    ret_id, line_id = ret.pk, line.pk
    source_lot_id = data["lot"].pk
    return_count = StockReturn.objects.count()
    line_count = StockReturnLine.objects.count()
    action_count = WarehouseAction.objects.count()
    old_targets = [
        ("actions", "0006_clear_legacy_application_area"),
        ("returns", "0002_seed_return_sequence"),
    ]
    leaf_targets = MigrationExecutor(connection).loader.graph.leaf_nodes()

    try:
        executor = MigrationExecutor(connection)
        executor.migrate(old_targets)
        old_apps = executor.loader.project_state(old_targets).apps
        OldRepairOrder = old_apps.get_model("repairs", "RepairOrder")
        OldRepairIssueLine = old_apps.get_model("repairs", "RepairIssueLine")
        OldStockReturn = old_apps.get_model("returns", "StockReturn")
        OldStockReturnLine = old_apps.get_model("returns", "StockReturnLine")
        assert OldRepairOrder.objects.get(pk=data["order"].pk).status == "completed"
        assert OldRepairIssueLine.objects.filter(pk=data["repair_lot_line"].pk).exists()
        assert OldStockReturn.objects.get(pk=ret_id).status == "draft"
        assert OldStockReturnLine.objects.get(pk=line_id).restock_status == "quarantine"

        MigrationExecutor(connection).migrate(
            [("returns", "0003_stockreturn_completed_by_and_more"),
             ("actions", "0007_warehouseaction_repair_issue_line_and_more")]
        )
        ret = StockReturn.objects.get(pk=ret_id)
        line = StockReturnLine.objects.get(pk=line_id)
        assert ret.status == StockReturn.Status.DRAFT
        assert line.restock_status == StockReturnLine.RestockStatus.QUARANTINE
        assert line.stock_lot_id == source_lot_id
        assert StockReturn.objects.count() == return_count
        assert StockReturnLine.objects.count() == line_count
        assert WarehouseAction.objects.count() == action_count

        movements_before = StockMovement.objects.count()
        update_return_line_restock_status(
            line, restock_status=StockReturnLine.RestockStatus.AVAILABLE, by=data["admin"]
        )
        assert StockMovement.objects.count() == movements_before
        complete_return(ret, by=data["admin"])
        ret.refresh_from_db()
        line.refresh_from_db()
        assert ret.status == StockReturn.Status.COMPLETED
        assert ret.completed_by_id == data["admin"].pk
        assert line.returned_lot_id == source_lot_id
        assert StockReturn.objects.count() == return_count
        assert StockReturnLine.objects.count() == line_count
        assert WarehouseAction.objects.filter(stock_return=ret).count() == 1
    finally:
        MigrationExecutor(connection).migrate(leaf_targets)


def _repair_return_action_for_part(data, part):
    batch_line = _finalized_line(data["sup"], part, data["admin"], qty="1")
    stock_lot = create_stock_lot(batch_line, data["loc"], Decimal("1"))
    receive_stock_lot(stock_lot, by=data["admin"])
    repair_order = create_repair_order(customer_name=part.name, by=data["admin"])
    repair_line = add_stock_lot_to_repair_order(
        repair_order, stock_lot, Decimal("1"), by=data["admin"]
    )
    repair_order = complete_repair_order(repair_order, by=data["admin"])
    ret = _new_return(data, repair_order)
    add_repair_line_return(
        ret,
        repair_line,
        Decimal("1"),
        to_location=data["loc2"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    return WarehouseAction.objects.get(stock_return=ret)


def test_repair_return_action_keeps_canonical_part_number_hierarchy(data):
    brp_part = promote_brp_to_warehouse(
        BrpCatalogPart.objects.create(
            material_no="420931285",
            part_desc="BRP identity",
            replacement_no_1="420931284",
            retail_price_usd=Decimal("10"),
        ),
        by=data["admin"],
    )
    polaris_part = promote_polaris_to_warehouse(
        PolarisCatalogPart.objects.create(
            part_number="POL-100", part_name="Polaris identity", superseded_number="POL-OLD"
        ),
        by=data["admin"],
    )
    oem_part = PartType.objects.create(
        name="OEM identity",
        category=data["lot"].part_type.category,
        unit=data["lot"].part_type.unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=oem_part, value="OEM-100", kind=PartNumber.Kind.OEM, is_primary=True
    )
    PartNumber.objects.create(part=oem_part, value="ANALOG-100", kind=PartNumber.Kind.ANALOG)
    PartNumber.objects.create(
        part=oem_part, value="INT-100", kind=PartNumber.Kind.INTERNAL_REF
    )
    article_part = PartType.objects.create(
        name="Article identity",
        category=data["lot"].part_type.category,
        unit=data["lot"].part_type.unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=article_part, value="ARTICLE-100", kind=PartNumber.Kind.ARTICLE
    )
    PartNumber.objects.create(
        part=article_part, value="ANALOG-200", kind=PartNumber.Kind.ANALOG
    )
    PartNumber.objects.create(
        part=article_part, value="INT-200", kind=PartNumber.Kind.INTERNAL_REF
    )
    no_exact_part = PartType.objects.create(
        name="No exact identity",
        category=data["lot"].part_type.category,
        unit=data["lot"].part_type.unit,
        tracking_mode=PartType.TrackingMode.BULK,
    )
    PartNumber.objects.create(
        part=no_exact_part, value="ANALOG-ONLY", kind=PartNumber.Kind.ANALOG
    )
    PartNumber.objects.create(
        part=no_exact_part, value="INT-ONLY", kind=PartNumber.Kind.INTERNAL_REF
    )

    cases = (
        (brp_part, "420931285", "420931284"),
        (polaris_part, "POL-100", "POL-OLD"),
        (oem_part, "OEM-100", "ANALOG-100"),
        (article_part, "ARTICLE-100", "ANALOG-200"),
        (no_exact_part, "Артикул не указан", "ANALOG-ONLY"),
    )
    for part, expected, rejected in cases:
        action = _repair_return_action_for_part(data, part)
        assert action.part_number == expected
        assert action.part_number != rejected


def test_repair_return_partial_sources_are_restored_independently(data):
    line = _finalized_line(data["sup"], data["lot"].part_type, data["admin"], qty="6")
    lot_a = create_stock_lot(line, data["loc"], Decimal("2"))
    lot_b = create_stock_lot(line, data["loc2"], Decimal("4"))
    receive_stock_lot(lot_a, by=data["admin"])
    receive_stock_lot(lot_b, by=data["admin"])
    order = create_repair_order(customer_name="Два источника", by=data["admin"])
    issue_a = add_stock_lot_to_repair_order(order, lot_a, Decimal("1"), by=data["admin"])
    issue_b = add_stock_lot_to_repair_order(order, lot_b, Decimal("2"), by=data["admin"])
    order = complete_repair_order(order, by=data["admin"])
    ret = _new_return(data, order)
    add_repair_line_return(
        ret,
        issue_a,
        Decimal("1"),
        to_location=data["loc2"],
        restock_status="available",
        by=data["admin"],
    )
    add_repair_line_return(
        ret,
        issue_b,
        Decimal("2"),
        to_location=data["loc"],
        restock_status="available",
        by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    lot_a.refresh_from_db()
    lot_b.refresh_from_db()
    assert lot_a.quantity == Decimal("2")
    assert lot_b.quantity == Decimal("4")
    assert returnable_quantity(issue_a) == Decimal("0")
    assert returnable_quantity(issue_b) == Decimal("0")
    order.refresh_from_db()
    assert order.cost_total == Decimal("0")


def test_repair_return_is_visible_in_actions_report_but_not_customs(data, client):
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret, data["repair_lot_line"], Decimal("1"), to_location=data["loc"],
        restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    actions, _totals = actions_report(action_type=WarehouseAction.Type.REPAIR_RETURN)
    action = actions.get()
    assert action.stock_return_id == ret.pk
    client.force_login(data["admin"])
    html = client.get(reverse("actions_report")).content.decode()
    assert ret.number in html
    assert data["order"].number in html
    assert "Возврат из ремонта" in html
    with patch("apps.actions.services.export_customs_xlsx", return_value=BytesIO()) as export:
        assert client.get(reverse("actions_export")).status_code == 200
    exported_actions = export.call_args.args[0]
    assert not exported_actions.filter(pk=action.pk).exists()


def test_repair_return_draft_detail_shows_source_and_expected_cost(data, client):
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret, data["repair_lot_line"], Decimal("1"), to_location=data["loc2"],
        restock_status="available", by=data["admin"],
    )
    client.force_login(data["admin"])
    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    assert "Черновик" in html
    assert "Нет" in html
    assert data["order"].number in html
    assert data["loc"].code in html
    assert "Будет восстановлено" in html
    assert "лот #" not in html.lower()


def test_return_complete_button_has_scoped_visible_styles_and_double_post_guard(data, client):
    ret = _new_return(data, data["order"])
    add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc"],
        restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=data["admin"],
    )
    client.force_login(data["admin"])
    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    css = Path("static/css/app.css").read_text(encoding="utf-8")

    assert 'class="btn btn--primary return-complete-button"' in html
    assert "data-return-complete-form" in html
    assert "button.disabled = true" in html
    assert ".return-complete-form .return-complete-button" in css
    assert "background: var(--accent); border-color: var(--accent); color: #fff;" in css


def test_return_complete_button_is_hidden_for_empty_draft(data, client):
    ret = _new_return(data, data["order"])
    client.force_login(data["admin"])

    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()

    assert "Провести возврат" not in html


def test_return_list_query_count_is_bounded(data, client):
    for _ in range(3):
        _new_return(data, data["order"])
    client.force_login(data["admin"])
    with CaptureQueriesContext(connection) as queries:
        response = client.get(reverse("return_list"))
    assert response.status_code == 200
    assert len(queries) <= 10


def _repair_return_draft_with_lines(data, line_count):
    repair_order = create_repair_order(customer_name=f"Запросы {line_count}", by=data["admin"])
    repair_lines = []
    for _ in range(line_count):
        batch_line = _finalized_line(
            data["sup"], data["lot"].part_type, data["admin"], qty="1"
        )
        stock_lot = create_stock_lot(batch_line, data["loc"], Decimal("1"))
        receive_stock_lot(stock_lot, by=data["admin"])
        repair_lines.append(
            add_stock_lot_to_repair_order(
                repair_order, stock_lot, Decimal("1"), by=data["admin"]
            )
        )
    repair_order = complete_repair_order(repair_order, by=data["admin"])
    ret = _new_return(data, repair_order)
    for repair_line in repair_lines:
        add_repair_line_return(
            ret,
            repair_line,
            Decimal("1"),
            to_location=data["loc2"],
            restock_status=StockReturnLine.RestockStatus.AVAILABLE,
            by=data["admin"],
        )
    return ret, repair_order


def _query_count(client, url):
    with CaptureQueriesContext(connection) as queries:
        response = client.get(url)
    assert response.status_code == 200
    return len(queries)


def test_repair_return_views_do_not_scale_queries_by_line_count(data, client):
    small_return, small_repair = _repair_return_draft_with_lines(data, 3)
    large_return, large_repair = _repair_return_draft_with_lines(data, 6)
    client.force_login(data["admin"])

    small_return_queries = _query_count(client, reverse("return_detail", args=[small_return.pk]))
    large_return_queries = _query_count(client, reverse("return_detail", args=[large_return.pk]))
    small_repair_queries = _query_count(
        client, reverse("repair_order_detail", args=[small_repair.pk])
    )
    large_repair_queries = _query_count(
        client, reverse("repair_order_detail", args=[large_repair.pk])
    )
    assert large_return_queries <= small_return_queries + 3
    assert large_repair_queries <= small_repair_queries + 3

    complete_return(small_return, by=data["admin"])
    small_report_queries = _query_count(
        client,
        f"{reverse('actions_report')}?action_type={WarehouseAction.Type.REPAIR_RETURN}",
    )
    complete_return(large_return, by=data["admin"])
    large_report_queries = _query_count(
        client,
        f"{reverse('actions_report')}?action_type={WarehouseAction.Type.REPAIR_RETURN}",
    )
    assert large_report_queries <= small_report_queries + 3


def _csrf_token(html):
    match = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', html)
    assert match is not None
    return match.group(1)


def _http_get(opener, url):
    return opener.open(url, timeout=10).read().decode()


def _http_post(opener, url, html, data):
    data = {**data, "csrfmiddlewaretoken": _csrf_token(html)}
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(data).encode(),
        headers={"Referer": url},
    )
    return opener.open(request, timeout=10).read().decode()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_repair_return_live_http_smoke(data, live_server):
    """Exercise the real local HTTP stack, including login and CSRF-protected changes."""
    ret = _new_return(data, data["order"])
    line = add_repair_line_return(
        ret,
        data["repair_lot_line"],
        Decimal("1"),
        to_location=data["loc2"],
        restock_status=StockReturnLine.RestockStatus.QUARANTINE,
        by=data["admin"],
    )
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    base_url = live_server.url
    login_url = f"{base_url}{reverse('login')}"
    login_page = _http_get(opener, login_url)
    _http_post(
        opener,
        login_url,
        login_page,
        {"username": "admin", "password": PASSWORD},
    )

    list_page = _http_get(opener, f"{base_url}{reverse('return_list')}")
    assert ret.number in list_page
    detail_url = f"{base_url}{reverse('return_detail', args=[ret.pk])}"
    draft_page = _http_get(opener, detail_url)
    assert "Черновик" in draft_page
    assert data["order"].number in draft_page
    assert data["loc"].code in draft_page
    changed_page = _http_post(
        opener,
        f"{base_url}{reverse('return_update_line_status', args=[line.pk])}",
        draft_page,
        {"restock_status": StockReturnLine.RestockStatus.AVAILABLE},
    )
    assert "Доступен" in changed_page

    repair_page = _http_get(
        opener, f"{base_url}{reverse('repair_order_detail', args=[data['order'].pk])}"
    )
    assert data["order"].number in repair_page
    completed_page = _http_post(
        opener,
        f"{base_url}{reverse('return_complete', args=[ret.pk])}",
        changed_page,
        {},
    )
    assert "Проведён" in completed_page
    report_page = _http_get(opener, f"{base_url}{reverse('actions_report')}")
    assert ret.number in report_page
    assert "Возврат из ремонта" in report_page
    balance_page = _http_get(opener, f"{base_url}{reverse('balance_list')}")
    assert data["loc"].code in balance_page
    lot_page = _http_get(opener, f"{base_url}{reverse('lot_detail', args=[data['lot'].pk])}")
    assert data["loc"].code in lot_page


def test_depleted_lot_revived_on_return(data):
    data["lot_small"].refresh_from_db()
    assert data["lot_small"].status == StockLot.Status.DEPLETED  # продан целиком
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_small_line"], Decimal("2"),
        to_location=data["loc"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["lot_small"].refresh_from_db()
    assert data["lot_small"].status == StockLot.Status.AVAILABLE
    assert data["lot_small"].quantity == Decimal("2")


def test_new_lot_when_returning_to_empty_cell(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc2"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    new_lot = StockLot.objects.get(batch_line=data["lot"].batch_line, location=data["loc2"])
    assert new_lot.pk != data["lot"].pk
    assert new_lot.quantity == Decimal("2")


def test_cannot_mix_quarantine_and_available_in_cell(data):
    # В loc уже есть available-лот (data["lot"]). Возврат в loc как quarantine → конфликт.
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    with pytest.raises(ReturnError):
        complete_return(ret, by=data["admin"])
    data["lot"].refresh_from_db()
    # Откат: количество не изменилось.
    assert data["lot"].quantity == Decimal("5")


# --- Инварианты «не больше / не дважды / ячейка» -----------------------------


def test_cannot_return_more_than_sold(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, data["sale_lot_line"], Decimal("4"),  # продано всего 3
            to_location=data["loc"], restock_status="available", by=data["admin"],
        )


def test_cannot_return_item_twice(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    ret2 = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret2, data["sale_item_line"], Decimal("1"),
            to_location=data["loc"], restock_status="quarantine", by=data["admin"],
        )


def test_cannot_complete_return_twice(data):
    ret = _new_return(data, data["sale"])
    line = add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    with pytest.raises(ReturnError):
        complete_return(ret, by=data["admin"])
    with pytest.raises(ReturnError):
        remove_return_line(line, by=data["admin"])


def test_cannot_return_to_storage_not_allowed(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, data["sale_item_line"], Decimal("1"),
            to_location=data["bad_loc"], restock_status="quarantine", by=data["admin"],
        )


def test_cannot_return_to_inactive_location(data):
    ret = _new_return(data, data["sale"])
    with pytest.raises(ReturnError):
        add_sale_line_return(
            ret, data["sale_item_line"], Decimal("1"),
            to_location=data["inactive_loc"], restock_status="quarantine", by=data["admin"],
        )


def test_return_part_item_service_rejects_available(data):
    # Прямой вызов физического сервиса на доступном экземпляре — нельзя вернуть.
    from apps.inventory.services import return_part_item

    available_item = data["item_a"]  # ещё sold
    return_part_item(
        available_item, data["loc"], restock_status="quarantine", by=data["admin"]
    )
    available_item.refresh_from_db()
    with pytest.raises(InventoryError):
        return_part_item(
            available_item, data["loc"], restock_status="available", by=data["admin"]
        )


# --- Себестоимость -----------------------------------------------------------


def test_return_freezes_cost_from_source(data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_lot_line"], Decimal("2"),
        to_location=data["loc2"], restock_status="available", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    line = ret.lines.get()
    assert line.unit_cost_rub == Decimal("104.00")          # из SaleLine, не текущий landed
    assert line.total_cost_rub == Decimal("208.00")
    ret.refresh_from_db()
    assert ret.cost_total == Decimal("208.00")


# --- Границы: возврат — не продажа / не оплата / не сторно --------------------


def test_return_does_not_create_sale(data):
    sales_before = Sale.objects.count()
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    assert Sale.objects.count() == sales_before


def test_return_is_not_payment(data):
    field_names = {f.name for f in StockReturn._meta.get_fields()}
    forbidden = {"paid", "payment", "payment_method", "receipt", "cash", "card", "refund"}
    assert field_names.isdisjoint(forbidden)


def test_sale_and_repair_unchanged_after_return(data):
    sale_revenue = data["sale"].revenue_total
    sale_profit = data["sale"].profit_total
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    data["sale"].refresh_from_db()
    data["order"].refresh_from_db()
    assert data["sale"].status == Sale.Status.COMPLETED
    assert data["sale"].revenue_total == sale_revenue
    assert data["sale"].profit_total == sale_profit
    assert data["order"].status == RepairOrder.Status.COMPLETED


# --- Права / себестоимость ----------------------------------------------------


def test_return_list_requires_login(client):
    assert client.get(reverse("return_list")).status_code == 302


def test_storekeeper_can_complete_return(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.post(reverse("return_complete", args=[ret.pk]))
    assert resp.status_code == 302
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.COMPLETED


def test_seller_cannot_complete_return(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("return_list")).status_code == 200  # просмотр ок
    assert client.post(reverse("return_complete", args=[ret.pk])).status_code == 403
    ret.refresh_from_db()
    assert ret.status == StockReturn.Status.DRAFT


def test_cost_hidden_without_capability(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    make_user("sklad", role=roles.STOREKEEPER)  # имеет manage_returns, но не purchase_cost
    client.login(username="sklad", password=PASSWORD)
    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    assert "Себестоимость" not in html


def test_cost_visible_for_manager(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    complete_return(ret, by=data["admin"])
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("return_detail", args=[ret.pk])).content.decode()
    assert "Себестоимость" in html


# --- Архитектура: view не пишет ledger ---------------------------------------


def test_view_delegates_to_service_without_writing_ledger(make_user, client, data):
    ret = _new_return(data, data["sale"])
    add_sale_line_return(
        ret, data["sale_item_line"], Decimal("1"),
        to_location=data["loc"], restock_status="quarantine", by=data["admin"],
    )
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    m_before = StockMovement.objects.count()
    b_before = sorted(StockBalance.objects.values_list("id", "quantity_physical"))
    with patch("apps.returns.views.complete_return") as mock_complete:
        client.post(reverse("return_complete", args=[ret.pk]))
    mock_complete.assert_called_once()
    assert StockMovement.objects.count() == m_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_physical")) == b_before


def test_untrusted_params_rechecked(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Несуществующий возврат → 404.
    assert client.post(reverse("return_complete", args=[999999])).status_code == 404
    # Подмена строки-источника на несуществующий id → ошибка, без эффекта.
    ret = _new_return(data, data["sale"])
    resp = client.post(
        reverse("return_add_line", args=[ret.pk]),
        {"source_line_id": 999999, "to_location": data["loc"].pk,
         "restock_status": "quarantine", "quantity": "1"},
    )
    assert resp.status_code == 302
    assert not ret.lines.exists()
