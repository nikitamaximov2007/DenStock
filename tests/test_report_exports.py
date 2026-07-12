"""Слой 22 — экспорт отчётов в CSV.

Покрывает план 22-layer-22-report-exports.md §16. Ключевое: экспорт использует ТЕ ЖЕ
reports.services, что UI (цифры не разъезжаются), те же права (VIEW_REPORTS) и
скрытие финансов (can_view_purchase_cost) — причём финансовые колонки ФИЗИЧЕСКИ не
пишутся в файл. Экспорт read-only. Формат: UTF-8 BOM, разделитель «;», Decimal «900,00».
"""
from datetime import timedelta
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
from apps.reports.exporters import sales_rows
from apps.reports.services import Period, get_sales_report
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import Sale
from apps.sales.services import (
    add_part_item_to_sale,
    add_stock_lot_to_sale,
    complete_sale,
    create_sale,
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


def _finalized_line(sup, part, admin, *, qty):
    batch = Batch.objects.create(supplier=sup, shipping_cost=Decimal("40"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part, quantity=Decimal(qty), unit_cost_currency=Decimal("100"),
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
    serial = PartType.objects.create(
        name="Деталь-А", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.SERIAL,
        min_price=Decimal("100"),
    )
    bulk = PartType.objects.create(
        name="Деталь-Б", category=cat, unit=unit, tracking_mode=PartType.TrackingMode.BULK,
        min_stock_level=Decimal("100"),
    )
    ilA = _finalized_line(sup, serial, admin, qty="2")
    item_a = create_part_items(ilA, 1, serial_number="A1")[0]
    receive_part_item(item_a, to_location=loc, by=admin)  # landed 120
    lot_sale = create_stock_lot(_finalized_line(sup, bulk, admin, qty="10"), loc, Decimal("5"))
    receive_stock_lot(lot_sale, by=admin)  # landed 104
    lot_wo = create_stock_lot(_finalized_line(sup, bulk, admin, qty="10"), loc, Decimal("5"))
    receive_stock_lot(lot_wo, by=admin)

    # Продажа: item_a (500) + lot_sale × 2 (200) → выручка 900, себест. 328, прибыль 572.
    sale = create_sale(customer_name="Покупатель", by=admin)
    add_part_item_to_sale(sale, item_a, unit_price=Decimal("500"), by=admin)
    add_stock_lot_to_sale(sale, lot_sale, Decimal("2"), unit_price=Decimal("200"), by=admin)
    complete_sale(sale, by=admin)
    sale.refresh_from_db()

    # Возврат проданного item_a (отдельно; выручку не трогает).
    ret = create_return(source=sale, by=admin)
    add_sale_line_return(
        ret, sale.lines.get(part_item=item_a), Decimal("1"),
        to_location=loc, restock_status="quarantine", by=admin,
    )
    complete_return(ret, by=admin)

    # Списание lot_wo × 3, причина «Брак».
    wo = create_write_off(reason=WriteOffDocument.Reason.DEFECT, by=admin)
    add_stock_lot_to_write_off(wo, lot_wo, Decimal("3"), by=admin)
    complete_write_off(wo, by=admin)

    return {"admin": admin, "loc": loc, "sale": sale}


def _csv_text(resp) -> str:
    return resp.content.decode("utf-8")


# --- Доступ / права ----------------------------------------------------------


def test_export_requires_login(client):
    assert client.get(reverse("reports_export_sales")).status_code == 302


def test_manager_can_export_sales(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    resp = client.get(reverse("reports_export_sales"))
    assert resp.status_code == 200
    assert resp["Content-Type"].startswith("text/csv")


def test_seller_cannot_export(make_user, client, data):
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    assert client.get(reverse("reports_export_sales")).status_code == 403


# --- Финансовая безопасность (физическое отсутствие колонок) -----------------


def test_manager_export_has_financial_columns(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _csv_text(client.get(reverse("reports_export_sales")))
    assert "Выручка (₽)" in text
    assert "Себестоимость (₽)" in text
    assert "Валовая прибыль (₽)" in text
    assert "900" in text
    assert "900,00" not in text


def test_storekeeper_export_omits_financials(make_user, client, data):
    make_user("sklad", role=roles.STOREKEEPER)
    client.login(username="sklad", password=PASSWORD)
    text = _csv_text(client.get(reverse("reports_export_sales")))
    # Ни заголовков, ни значений финансов в файле.
    assert "Выручка" not in text
    assert "Себестоимость" not in text
    assert "Прибыль" not in text
    assert "900,00" not in text
    assert "572,00" not in text
    # Складские (нефинансовые) колонки на месте.
    assert "Продаж" in text


# --- Формат CSV --------------------------------------------------------------


def test_csv_has_bom(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    resp = client.get(reverse("reports_export_sales"))
    assert resp.content.startswith(b"\xef\xbb\xbf")  # UTF-8 BOM


def test_csv_has_russian_headers_and_semicolon(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _csv_text(client.get(reverse("reports_export_sales")))
    assert "Период с" in text
    assert ";" in text  # разделитель


def test_filename_in_content_disposition(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    resp = client.get(reverse("reports_export_stock"))
    cd = resp["Content-Disposition"]
    assert "denstock-stock-" in cd
    assert cd.endswith('.csv"')


# --- Период / согласованность с UI -------------------------------------------


def test_sales_export_respects_period(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Прошлый период → продаж нет, суммы 900 в файле быть не должно.
    text = _csv_text(
        client.get(reverse("reports_export_sales") + "?date_from=2020-01-01&date_to=2020-12-31")
    )
    assert "900,00" not in text


def test_returns_export_does_not_subtract_from_revenue(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    # Возврат существует, но выручка в экспорте продаж не уменьшена.
    sales_text = _csv_text(client.get(reverse("reports_export_sales")))
    assert "900" in sales_text
    assert "900,00" not in sales_text


def test_export_matches_service_numbers(data):
    # Экспортер берёт ровно те числа, что отдаёт сервис (не считает сам).
    today = timezone.localdate()
    period = Period(today - timedelta(days=30), today, "30")
    report = get_sales_report(period)
    header, rows = sales_rows(report, period, include_costs=True)
    assert "Выручка (₽)" in header
    idx = header.index("Выручка (₽)")
    assert rows[0][idx] == "900"


# --- Складские экспорты ------------------------------------------------------


def test_stock_export_columns(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _csv_text(client.get(reverse("reports_export_stock")))
    assert "Доступно" in text
    assert "Зарезервировано" in text
    assert "Карантин" in text
    assert "ИТОГО" in text


def test_low_stock_export_columns(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _csv_text(client.get(reverse("reports_export_low_stock")))
    assert "Деталь" in text
    assert "Минимум" in text
    assert "Деталь-Б" in text  # available 8 < min 100


def test_writeoffs_export_by_reason(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    text = _csv_text(client.get(reverse("reports_export_writeoffs")))
    assert "Причина" in text
    assert "Брак" in text


def test_repairs_and_stocktaking_export_ok(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    assert client.get(reverse("reports_export_repairs")).status_code == 200
    assert client.get(reverse("reports_export_stocktaking")).status_code == 200
    assert client.get(reverse("reports_export_returns")).status_code == 200


# --- Read-only ---------------------------------------------------------------


def test_export_is_read_only(make_user, client, data):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    mv_before = StockMovement.objects.count()
    sale_before = Sale.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    for name in [
        "reports_export_sales", "reports_export_returns", "reports_export_repairs",
        "reports_export_writeoffs", "reports_export_stocktaking", "reports_export_stock",
        "reports_export_low_stock",
    ]:
        client.get(reverse(name))
    assert StockMovement.objects.count() == mv_before
    assert Sale.objects.count() == sale_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before


def test_no_pending_migrations(db):
    out = StringIO()
    try:
        call_command("makemigrations", "--check", "--dry-run", stdout=out, stderr=out)
    except SystemExit:
        pytest.fail(f"Есть несозданные миграции:\n{out.getvalue()}")
