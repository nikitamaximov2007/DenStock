from datetime import datetime, time, timedelta
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.inventory.models import StockBalance, StockLot, StockMovement
from apps.procurement.models import Batch, BatchLine
from apps.reports.services import (
    Period,
    attach_customer_part_identity,
    get_customer_part_operations,
    get_customer_part_sales,
    get_sales_by_customer,
)
from apps.sales.models import Sale, SaleLine
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import create_location

PASSWORD = "parol-12345"


@pytest.fixture
def report_data(db, django_user_model):
    admin = django_user_model.objects.create_superuser("sales-report-admin", password=PASSWORD)
    manager = django_user_model.objects.create_user("sales-report-manager", password=PASSWORD)
    manager.groups.add(Group.objects.get(name=roles.MANAGER))
    storekeeper = django_user_model.objects.create_user(
        "sales-report-storekeeper", password=PASSWORD
    )
    storekeeper.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    seller = django_user_model.objects.create_user("sales-report-seller", password=PASSWORD)
    seller.groups.add(Group.objects.get(name=roles.SELLER))

    category = Category.objects.create(name="Отчёт продаж")
    unit = Unit.objects.get(name="Штука")
    parts = [
        PartType.objects.create(
            name="Фильтр",
            category=category,
            unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal("999.00"),
        ),
        PartType.objects.create(
            name="Прокладка",
            category=category,
            unit=unit,
            tracking_mode=PartType.TrackingMode.BULK,
            recommended_price=Decimal("888.00"),
        ),
    ]
    PartNumber.objects.create(
        part=parts[0], value="P-100", kind=PartNumber.Kind.OEM, is_primary=True
    )
    PartNumber.objects.create(
        part=parts[1], value="P-200", kind=PartNumber.Kind.OEM, is_primary=True
    )
    supplier = Supplier.objects.create(name="Поставщик отчёта")
    batch = Batch.objects.create(supplier=supplier)
    location = create_location("S09-D01-C01")
    lots = []
    lines = []
    for part in parts:
        batch_line = BatchLine.objects.create(
            batch=batch,
            part_type=part,
            quantity=Decimal("100"),
            unit_cost_currency=Decimal("10"),
        )
        lines.append(batch_line)
        lots.append(
            StockLot.objects.create(
                part_type=part,
                batch=batch,
                batch_line=batch_line,
                location=location,
                quantity=Decimal("100"),
                initial_quantity=Decimal("100"),
                landed_unit_cost_rub=Decimal("10"),
                status=StockLot.Status.AVAILABLE,
            )
        )

    def make_sale(
        customer,
        sale_lines,
        *,
        status=Sale.Status.COMPLETED,
        sold_at=None,
        sold_by=admin,
    ):
        sold_at = sold_at if sold_at is not None else timezone.now()
        revenue = sum(
            (Decimal(quantity) * Decimal(unit_price) for _, quantity, unit_price in sale_lines),
            Decimal("0"),
        )
        sale = Sale.objects.create(
            status=status,
            customer_name=customer,
            sold_by=sold_by,
            sold_at=sold_at,
            revenue_total=revenue,
        )
        for part_index, quantity, unit_price in sale_lines:
            quantity = Decimal(quantity)
            unit_price = Decimal(unit_price)
            SaleLine.objects.create(
                sale=sale,
                part_type=parts[part_index],
                stock_lot=lots[part_index],
                batch=batch,
                batch_line=lines[part_index],
                quantity=quantity,
                unit_price=unit_price,
                total_price=quantity * unit_price,
            )
        return sale

    ivan_first = make_sale("Иван Иванов", [(0, "2", "100"), (1, "1", "50")])
    ivan_second = make_sale("Иван Иванов", [(0, "1", "150")])
    make_sale("Пётр Петров", [(0, "3", "90")])
    make_sale("", [(1, "1", "75")])
    make_sale("Черновик", [(0, "20", "1000")], status=Sale.Status.DRAFT, sold_at=None)
    make_sale("Отменён", [(0, "20", "1000")], status=Sale.Status.CANCELED)
    make_sale("Сторно", [(0, "20", "1000")], status=Sale.Status.VOIDED)
    make_sale(
        "Старая продажа",
        [(0, "20", "1000")],
        sold_at=timezone.now() - timedelta(days=365),
    )
    today = timezone.localdate()
    return {
        "admin": admin,
        "manager": manager,
        "storekeeper": storekeeper,
        "seller": seller,
        "parts": parts,
        "lots": lots,
        "batch": batch,
        "batch_lines": lines,
        "make_sale": make_sale,
        "ivan_sales": [ivan_first, ivan_second],
        "period": Period(today - timedelta(days=29), today, "30"),
    }


def test_customer_summary_uses_only_completed_snapshot_lines(report_data):
    rows = {row["report_customer"]: row for row in get_sales_by_customer(report_data["period"])}
    assert set(rows) == {"", "Иван Иванов", "Пётр Петров"}
    ivan = rows["Иван Иванов"]
    assert ivan["sale_count"] == 2
    assert ivan["unique_parts"] == 2
    assert ivan["quantity"] == Decimal("4")
    assert ivan["revenue"] == Decimal("400")
    assert rows[""]["revenue"] == Decimal("75")


def test_customer_parts_and_operations_keep_historical_prices(report_data):
    part = report_data["parts"][0]
    part.recommended_price = Decimal("99999")
    part.save(update_fields=["recommended_price"])

    rows = attach_customer_part_identity(
        get_customer_part_sales(
            report_data["period"], customer_name="Иван Иванов", missing=False
        )
    )
    filter_row = next(row for row in rows if row["part_type_id"] == part.pk)
    assert filter_row["exact_number"] == "P-100"
    assert filter_row["quantity"] == Decimal("3")
    assert filter_row["revenue"] == Decimal("350")
    assert filter_row["operation_count"] == 2

    operations = list(
        get_customer_part_operations(
            report_data["period"],
            customer_name="Иван Иванов",
            missing=False,
            part_type_id=part.pk,
        )
    )
    assert {line.unit_price for line in operations} == {Decimal("100"), Decimal("150")}
    assert sum((line.total_price for line in operations), Decimal("0")) == Decimal("350")


def test_date_filter_includes_both_day_boundaries(report_data):
    day = timezone.localdate()
    start = timezone.make_aware(datetime.combine(day, time.min))
    end = timezone.make_aware(datetime.combine(day, time.max))
    report_data["make_sale"]("Граница", [(0, "1", "10")], sold_at=start)
    report_data["make_sale"]("Граница", [(0, "1", "20")], sold_at=end)
    rows = list(get_sales_by_customer(Period(day, day, "today")))
    boundary = next(row for row in rows if row["report_customer"] == "Граница")
    assert boundary["sale_count"] == 2
    assert boundary["revenue"] == Decimal("30")


def test_views_permissions_missing_customer_and_snapshot_detail(client, report_data):
    list_url = reverse("reports_sales_by_client")
    client.force_login(report_data["manager"])
    html = client.get(list_url).content.decode()
    assert "Иван Иванов" in html
    assert "Без клиента" in html
    assert "Сумма (₽)" in html

    missing = client.get(
        reverse("reports_sales_by_client_detail"),
        {"missing": "1", "preset": "30"},
    )
    assert missing.status_code == 200
    assert "Продажи клиенту: Без клиента" in missing.content.decode()

    operations = client.get(
        reverse("reports_sales_by_client_operations"),
        {"customer": "Иван Иванов", "part": report_data["parts"][0].pk, "preset": "30"},
    )
    operation_html = operations.content.decode()
    assert operations.status_code == 200
    assert "P-100" in operation_html
    assert "100" in operation_html
    assert "150" in operation_html

    client.force_login(report_data["storekeeper"])
    restricted_html = client.get(list_url).content.decode()
    assert "Иван Иванов" in restricted_html
    assert "Сумма (₽)" not in restricted_html
    client.force_login(report_data["seller"])
    assert client.get(list_url).status_code == 403
    assert (
        client.get(reverse("reports_sales_by_client_detail"), {"missing": "1"}).status_code
        == 403
    )
    assert (
        client.get(
            reverse("reports_sales_by_client_operations"),
            {"missing": "1", "part": report_data["parts"][0].pk},
        ).status_code
        == 403
    )


def test_nested_views_reject_incomplete_selection(client, report_data):
    client.force_login(report_data["manager"])
    assert client.get(reverse("reports_sales_by_client_detail")).status_code == 404
    assert (
        client.get(
            reverse("reports_sales_by_client_operations"),
            {"customer": "Иван Иванов", "part": "not-a-number"},
        ).status_code
        == 404
    )


def test_empty_report_and_pagination(client, report_data):
    client.force_login(report_data["manager"])
    empty = client.get(
        reverse("reports_sales_by_client"),
        {"date_from": "2020-01-01", "date_to": "2020-01-02"},
    )
    assert "Продаж за период нет" in empty.content.decode()

    for index in range(51):
        report_data["make_sale"](f"Клиент {index:02d}", [(0, "1", "1")])
    second_page = client.get(reverse("reports_sales_by_client"), {"preset": "30", "page": "2"})
    assert second_page.status_code == 200
    assert "Страница 2 из 2" in second_page.content.decode()


def test_report_is_read_only_and_list_query_count_is_constant(client, report_data):
    client.force_login(report_data["manager"])
    url = reverse("reports_sales_by_client") + "?preset=30"
    sale_before = Sale.objects.count()
    movement_before = StockMovement.objects.count()
    balances_before = list(StockBalance.objects.values_list("pk", "quantity_physical"))

    with CaptureQueriesContext(connection) as first:
        response = client.get(url)
    assert response.status_code == 200
    for index in range(20):
        report_data["make_sale"](f"Дополнительный {index:02d}", [(0, "1", "2")])
    expected_sales = Sale.objects.count()
    with CaptureQueriesContext(connection) as many:
        response = client.get(url)
    assert response.status_code == 200
    assert len(many) <= len(first) + 1
    assert Sale.objects.count() == expected_sales
    assert expected_sales == sale_before + 20
    assert StockMovement.objects.count() == movement_before
    assert list(StockBalance.objects.values_list("pk", "quantity_physical")) == balances_before
