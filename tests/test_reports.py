"""Слой 21 — read-only отчёты и базовая аналитика.

Покрывает план 21-layer-21-reports-analytics.md §20. Ключевое: отчёты только читают
уже созданные документы/движения/остатки и НИЧЕГО не меняют (ни StockMovement, ни
Sale/StockLot/StockBalance). Возвраты показаны отдельно и не вычитаются из выручки;
ADJUST_* отделены от WRITE_OFF_*; деньги — под can_view_purchase_cost.
"""
from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

import pytest
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockBalance, StockMovement
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.services import (
    add_part_item_to_repair_order,
    complete_repair_order,
    create_repair_order,
)
from apps.reports.services import (
    Period,
    get_dashboard_report,
    get_low_stock_report,
    get_sales_report,
    get_stock_report,
    get_stocktaking_report,
    get_writeoffs_report,
    resolve_period,
)
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import Sale
from apps.sales.services import (
    add_part_item_to_sale,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
)
from apps.stocktaking.services import (
    add_stock_lot_count_line,
    complete_inventory_count,
    create_inventory_count,
    update_counted_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    add_stock_lot_to_write_off,
    complete_write_off,
    create_write_off,
)

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


def _now_period():
    today = timezone.localdate()
    return Period(today - timedelta(days=30), today, "30")


@pytest.fixture
def data(db, admin):
    sup = Supplier.objects.create(name="ООО Поставка")
    cat = Category.objects.create(name="Двигатель")
    unit = Unit.objects.get(name="Штука")
    loc = StorageLocation.objects.create(
        name="Ячейка A", code="A-01", storage_allowed=True, is_active=True
    )
    serial = PartType.objects.create(
        name="Деталь-А", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL,
        recommended_price=Decimal("500"), min_price=Decimal("100"),
    )
    bulk = PartType.objects.create(
        name="Деталь-Б", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
        min_stock_level=Decimal("100"),  # заведомо низкий остаток
    )
    ilA1 = _finalized_line(sup, serial, admin, qty="2")
    item_a = create_part_items(ilA1, 1, serial_number="A1")[0]
    receive_part_item(item_a, to_location=loc, by=admin)  # landed 120
    ilA2 = _finalized_line(sup, serial, admin, qty="2")
    item_b = create_part_items(ilA2, 1, serial_number="A2")[0]
    receive_part_item(item_b, to_location=loc, by=admin)

    lot_sale = create_stock_lot(_finalized_line(sup, bulk, admin, qty="10"), loc, Decimal("5"))
    receive_stock_lot(lot_sale, by=admin)  # landed 104
    lot_wo = create_stock_lot(_finalized_line(sup, bulk, admin, qty="10"), loc, Decimal("5"))
    receive_stock_lot(lot_wo, by=admin)
    lot_inv = create_stock_lot(_finalized_line(sup, bulk, admin, qty="10"), loc, Decimal("5"))
    receive_stock_lot(lot_inv, by=admin)

    # Продажа: item_a (500) + lot_sale × 2 (200) → выручка 900, себест. 328, прибыль 572.
    sale = create_sale(customer_name="Покупатель", by=admin)
    add_part_item_to_sale(sale, item_a, unit_price=Decimal("500"), by=admin)
    add_stock_lot_to_sale(sale, lot_sale, Decimal("2"), unit_price=Decimal("200"), by=admin)
    complete_sale(sale, by=admin)
    sale.refresh_from_db()

    # Ремонт: выдать item_b (себест. 120).
    order = create_repair_order(customer_name="Клиент", by=admin)
    add_part_item_to_repair_order(order, item_b, by=admin)
    complete_repair_order(order, by=admin)

    # Возврат проданного item_a (кол-во 1, себест. 120) — отдельно от выручки.
    ret = create_return(source=sale, by=admin)
    add_sale_line_return(
        ret, sale.lines.get(part_item=item_a), Decimal("1"),
        to_location=loc, restock_status="quarantine", by=admin,
    )
    complete_return(ret, by=admin)

    # Списание: lot_wo × 3, причина «Брак» (себест. 312).
    wo = create_write_off(reason=WriteOffDocument.Reason.DEFECT, by=admin)
    add_stock_lot_to_write_off(wo, lot_wo, Decimal("3"), by=admin)
    complete_write_off(wo, by=admin)

    # Инвентаризация: lot_inv факт 3 при системных 5 → ADJUST_OUT 2 (себест. 208).
    ic = create_inventory_count(scope_location=loc, by=admin)
    line = add_stock_lot_count_line(ic, lot_inv, by=admin)
    update_counted_quantity(line, Decimal("3"), by=admin)
    complete_inventory_count(ic, by=admin)

    return {"admin": admin, "loc": loc, "sale": sale, "serial": serial, "bulk": bulk}


# --- Продажи -----------------------------------------------------------------


def test_sales_totals(data):
    rep = get_sales_report(_now_period())
    assert rep.count == 1
    assert rep.revenue == Decimal("900.00")
    assert rep.cost == Decimal("328.00")
    assert rep.profit == Decimal("572.00")


def test_sales_only_completed_in_period(data):
    # Черновик-продажа не считается.
    create_sale(customer_name="Черновик", by=data["admin"])
    rep = get_sales_report(_now_period())
    assert rep.count == 1  # только проведённая


def test_sales_out_of_period_excluded(data):
    past = Period(date(2020, 1, 1), date(2020, 12, 31), "")
    assert get_sales_report(past).count == 0


def test_top_by_revenue_and_quantity(data):
    rep = get_sales_report(_now_period())
    # По выручке лидирует Деталь-А (500 > 400).
    assert rep.top_by_revenue[0].part_type == "Деталь-А"
    # По количеству лидирует Деталь-Б (2 > 1).
    assert rep.top_by_quantity[0].part_type == "Деталь-Б"
    assert rep.top_by_quantity[0].value == Decimal("2.000")


# --- Возвраты / ремонт / списания / инвентаризация ---------------------------


def test_returns_separate_not_subtracted(data):
    dash = get_dashboard_report(_now_period())
    assert dash.returns.count == 1
    assert dash.returns.quantity == Decimal("1.000")
    assert dash.returns.cost == Decimal("120.00")
    # Возврат НЕ уменьшил выручку продаж.
    assert dash.sales.revenue == Decimal("900.00")


def test_repairs_counted_by_completed(data):
    dash = get_dashboard_report(_now_period())
    assert dash.repairs.count == 1
    assert dash.repairs.issued_cost == Decimal("120.00")


def test_writeoffs_grouped_by_reason(data):
    rep = get_writeoffs_report(_now_period())
    assert rep.count == 1
    assert rep.cost == Decimal("312.00")
    reasons = {row.reason: row for row in rep.by_reason}
    assert "Брак" in reasons
    assert reasons["Брак"].cost == Decimal("312.00")


def test_adjustments_separate_from_writeoffs(data):
    adj = get_stocktaking_report(_now_period())
    assert adj.count == 1
    assert adj.adjust_out_qty == Decimal("2.000")
    assert adj.adjust_out_cost == Decimal("208.00")
    # Списания не попали в корректировки и наоборот.
    wo = get_writeoffs_report(_now_period())
    assert wo.cost == Decimal("312.00")  # отдельно от 208 корректировки


# --- Остатки / низкие остатки ------------------------------------------------


def test_stock_report_totals(data):
    stock = get_stock_report()
    # Деталь-Б: lot_sale 3 + lot_wo 2 + lot_inv 3 = 8 доступно.
    assert stock.total_available >= Decimal("8")


def test_low_stock_uses_min_level(data):
    low = get_low_stock_report()
    names = {row.part_type for row in low}
    assert "Деталь-Б" in names  # available 8 < min 100
    row = next(r for r in low if r.part_type == "Деталь-Б")
    assert row.min_stock_level == Decimal("100.000")


# --- Read-only гарантия ------------------------------------------------------


def test_reports_are_read_only(data):
    mv_before = StockMovement.objects.count()
    sale_before = Sale.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    # Прогоняем все отчёты.
    get_dashboard_report(_now_period())
    get_stock_report()
    get_low_stock_report()
    assert StockMovement.objects.count() == mv_before
    assert Sale.objects.count() == sale_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before


def test_view_read_only(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    mv_before = StockMovement.objects.count()
    client.get(reverse("reports_dashboard"))
    client.get(reverse("reports_stock"))
    assert StockMovement.objects.count() == mv_before


# --- Права / себестоимость ----------------------------------------------------


def test_reports_require_login(client):
    assert client.get(reverse("reports_dashboard")).status_code == 302


def test_seller_has_no_reports(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("reports_dashboard")).status_code == 403


def test_manager_sees_reports_with_money(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    html = client.get(reverse("reports_dashboard")).content.decode()
    assert "Выручка" in html  # денежный блок показан (точные суммы — в сервис-тестах)
    assert "Валовая прибыль" in html


def test_storekeeper_sees_reports_without_money(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    resp = client.get(reverse("reports_dashboard"))
    assert resp.status_code == 200  # доступ есть
    html = resp.content.decode()
    assert "Выручка" not in html  # денежные блоки скрыты
    assert "900.00" not in html


# --- Период / архитектура ----------------------------------------------------


def test_period_validation_defaults(data):
    # Некорректные даты → дефолт (пресет «30»), страница не падает.
    p = resolve_period({"date_from": "не-дата", "date_to": ""})
    assert p.preset == "30"
    assert p.date_from <= p.date_to
    # from > to → нормализуется.
    p2 = resolve_period({"date_from": "2026-06-30", "date_to": "2026-06-01"})
    assert p2.date_from <= p2.date_to


def test_view_uses_service(make_user, client, data, monkeypatch):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    called = {}
    import apps.reports.views as views_mod

    real = views_mod.get_dashboard_report

    def spy(period):
        called["yes"] = True
        return real(period)

    monkeypatch.setattr(views_mod, "get_dashboard_report", spy)
    client.get(reverse("reports_dashboard"))
    assert called.get("yes") is True


def test_no_pending_migrations(db):
    out = StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    except SystemExit:
        pytest.fail(f"Есть несозданные миграции:\n{out.getvalue()}")
