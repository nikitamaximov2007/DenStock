"""Отмена отдельной проданной позиции прямо из отчёта по клиенту.

Оператору не нужно открывать карточку продажи и отменять её целиком: он видит
строку «продано 4» и отменяет из неё одну единицу. Отдельного складского
механизма для этого нет и не заводится - сторнирование идёт тем же документом
возврата, который уже умеет возвращать в исходный лот и не пускать вернуть
больше проданного.

Документ продажи при этом неизменен: его количество остаётся снимком, а
действующее считается вычитанием возвратов. Историю по-прежнему можно
доказать: продано четыре, отменена одна, осталось три.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import PartItem, StockMovement
from apps.inventory.services import (
    create_part_items,
    create_stock_lot,
    receive_part_item,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.returns.models import StockReturn, StockReturnLine
from apps.returns.services import add_sale_line_return, complete_return, create_return
from apps.sales.models import Sale
from apps.sales.services import (
    SaleError,
    add_part_item_to_sale,
    add_stock_lot_to_sale,
    cancel_sale,
    cancel_sale_line_quantity,
    complete_sale,
    create_sale,
    reversible_quantity,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


def _line(supplier, part, admin, *, quantity, unit_cost):
    batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, admin)
    line.refresh_from_db()
    return line


@pytest.fixture
def env(db, admin):
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Отмена позиции")
    unit = Unit.objects.get(name="Штука")
    first = StorageLocation.objects.create(
        name="Ячейка 1", code="P-01", storage_allowed=True, is_active=True
    )
    second = StorageLocation.objects.create(
        name="Ячейка 2", code="P-02", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="PISTON CIRCLIP", category=category, unit=unit,
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("554"),
    )
    cheap = create_stock_lot(_line(supplier, part, admin, quantity="10", unit_cost="100"),
                             first, Decimal("10"))
    receive_stock_lot(cheap, by=admin)
    dear = create_stock_lot(_line(supplier, part, admin, quantity="10", unit_cost="250"),
                            second, Decimal("10"))
    receive_stock_lot(dear, by=admin)
    return {
        "admin": admin, "supplier": supplier, "category": category, "unit": unit,
        "first": first, "second": second, "part": part, "cheap": cheap, "dear": dear,
    }


def _sold(env, *, quantity="4", price="554", customer=None, lot=None):
    sale = create_sale(customer_name="Иванов", by=env["admin"], customer=customer)
    add_stock_lot_to_sale(
        sale, lot or env["cheap"], Decimal(quantity),
        unit_price=Decimal(price), by=env["admin"],
    )
    return complete_sale(sale, by=env["admin"])


# --- Частичное сторнирование -------------------------------------------------------


def test_cancelling_one_of_four_leaves_three_in_effect(env):
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("6")

    cancel_sale_line_quantity(
        line, 1, reason="Клиент передумал", author="Иванов И.", by=env["admin"]
    )

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("7")  # одна вернулась
    line.refresh_from_db()
    assert line.quantity == Decimal("4")  # снимок продажи не переписан
    assert reversible_quantity(line) == Decimal("3")
    sale.refresh_from_db()
    assert sale.status == Sale.Status.COMPLETED  # продажа целиком не отменена


def test_the_reversal_returns_to_the_original_lot_and_cell(env):
    sale = _sold(env, quantity="4", lot=env["dear"])
    line = sale.lines.get()

    cancel_sale_line_quantity(line, 2, reason="Брак", author="Иванов И.", by=env["admin"])

    env["dear"].refresh_from_db()
    env["cheap"].refresh_from_db()
    assert env["dear"].quantity == Decimal("8")  # вернулось в свой лот
    assert env["dear"].location_id == env["second"].pk
    assert env["cheap"].quantity == Decimal("10")  # чужой лот не тронут
    movement = StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT
    ).order_by("-pk").first()
    assert movement.to_location_id == env["second"].pk
    assert movement.unit_cost_rub == line.unit_cost_rub  # себестоимость исходная


def test_cancelling_step_by_step_walks_the_line_down(env):
    sale = _sold(env, quantity="4")
    line = sale.lines.get()

    cancel_sale_line_quantity(line, 1, reason="Раз", author="И.", by=env["admin"])
    assert reversible_quantity(line) == Decimal("3")
    cancel_sale_line_quantity(line, 2, reason="Два", author="И.", by=env["admin"])
    assert reversible_quantity(line) == Decimal("1")
    cancel_sale_line_quantity(line, 1, reason="Три", author="И.", by=env["admin"])

    assert reversible_quantity(line) == Decimal("0")
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")


def test_more_than_remaining_is_refused(env):
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    cancel_sale_line_quantity(line, 3, reason="Часть", author="И.", by=env["admin"])

    with pytest.raises(SaleError):
        cancel_sale_line_quantity(line, 2, reason="Ещё", author="И.", by=env["admin"])

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("9")  # вернулись только три


def test_an_earlier_ordinary_return_is_counted(env):
    """Продано 4, возврат 1: отменить можно только оставшиеся 3."""
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    document = create_return(source=sale, reason="Не подошло", by=env["admin"])
    add_sale_line_return(
        document, line, Decimal("1"),
        to_location=env["first"], restock_status=StockReturnLine.RestockStatus.AVAILABLE,
        by=env["admin"],
    )
    complete_return(document, by=env["admin"])

    assert reversible_quantity(line) == Decimal("3")
    with pytest.raises(SaleError):
        cancel_sale_line_quantity(line, 4, reason="Всё", author="И.", by=env["admin"])
    cancel_sale_line_quantity(line, 3, reason="Остаток", author="И.", by=env["admin"])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")


def test_reason_and_author_are_required_and_recorded(env):
    sale = _sold(env, quantity="2")
    line = sale.lines.get()
    for reason, author in (("", "И."), ("Причина", ""), ("  ", " ")):
        with pytest.raises(SaleError):
            cancel_sale_line_quantity(line, 1, reason=reason, author=author, by=env["admin"])

    document = cancel_sale_line_quantity(
        line, 1, reason="Клиент вернул", author="Иванов И.", by=env["admin"]
    )

    assert "Клиент вернул" in document.reason
    assert "Иванов И." in document.comment
    assert document.created_by_id == env["admin"].pk  # аудит: кто нажал
    assert document.completed_by_id == env["admin"].pk


def test_a_draft_return_blocks_the_line_cancellation(env):
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    create_return(source=sale, reason="Разбираемся", by=env["admin"])

    with pytest.raises(SaleError):
        cancel_sale_line_quantity(line, 1, reason="Отмена", author="И.", by=env["admin"])


def test_only_a_completed_sale_allows_line_cancellation(env):
    draft = create_sale(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_sale(
        draft, env["cheap"], Decimal("2"), unit_price=Decimal("554"), by=env["admin"]
    )
    with pytest.raises(SaleError):
        cancel_sale_line_quantity(
            draft.lines.get(), 1, reason="Отмена", author="И.", by=env["admin"]
        )


# --- Совместимость с отменой документа целиком ---------------------------------------


def test_whole_sale_cancellation_returns_only_what_is_left(env):
    """Продано 4, одна отменена из отчёта: отмена документа вернёт три."""
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    cancel_sale_line_quantity(line, 1, reason="Одна", author="И.", by=env["admin"])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("7")

    cancel_sale(sale, by=env["admin"], reason="Остальное", author="Иванов И.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")  # 7 + 3, а не 7 + 4
    sale.refresh_from_db()
    assert sale.status == Sale.Status.CANCELED


def test_a_fully_reversed_line_leaves_nothing_for_the_document_cancellation(env):
    sale = _sold(env, quantity="3")
    line = sale.lines.get()
    cancel_sale_line_quantity(line, 3, reason="Всё", author="И.", by=env["admin"])
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")
    movements = StockMovement.objects.count()

    cancel_sale(sale, by=env["admin"], reason="Документ", author="Иванов И.")

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("10")  # второй раз ничего не вернулось
    assert StockMovement.objects.count() == movements


# --- Серийный экземпляр ----------------------------------------------------------------


def test_a_serial_line_goes_back_to_its_own_cell(env):
    serial = PartType.objects.create(
        name="НАСОС", category=env["category"], unit=env["unit"],
        tracking_mode=PartType.TrackingMode.SERIAL, recommended_price=Decimal("900"),
    )
    line = _line(env["supplier"], serial, env["admin"], quantity="1", unit_cost="400")
    item = create_part_items(line, 1, serial_number="SN-1")[0]
    receive_part_item(item, to_location=env["second"], by=env["admin"])
    sale = create_sale(customer_name="Иванов", by=env["admin"])
    add_part_item_to_sale(sale, item, unit_price=Decimal("1200"), by=env["admin"])
    complete_sale(sale, by=env["admin"])
    sale_line = sale.lines.get()

    cancel_sale_line_quantity(sale_line, 1, reason="Возврат", author="И.", by=env["admin"])

    item.refresh_from_db()
    assert item.status == PartItem.Status.AVAILABLE
    assert item.current_location_id == env["second"].pk
    assert reversible_quantity(sale_line) == Decimal("0")


# --- Отчёт ------------------------------------------------------------------------------


def _report(client, customer):
    return client.get(
        reverse("reports_sales_by_client_detail"), {"customer_id": customer.pk}
    ).content.decode()


def test_the_report_row_offers_cancellation_and_then_shows_the_effect(client, env):
    customer = Customer.objects.create(name="Саликов Рим Васильевич")
    sale = _sold(env, quantity="4", customer=customer)
    line = sale.lines.get()
    client.force_login(env["admin"])

    before = _report(client, customer)
    assert reverse("sale_line_cancel", args=[line.pk]) in before
    assert "Отменить" in before

    cancel_sale_line_quantity(line, 1, reason="Одна", author="И.", by=env["admin"])

    after = _report(client, customer)
    assert "продано 4, отменено 1" in after
    assert ">3<" in after.replace(" ", "").replace("\n", "")
    line.refresh_from_db()
    assert line.quantity == Decimal("4")  # история не переписана


def test_a_fully_reversed_row_shows_a_status_instead_of_a_button(client, env):
    customer = Customer.objects.create(name="Саликов Рим Васильевич")
    sale = _sold(env, quantity="2", customer=customer)
    line = sale.lines.get()
    cancel_sale_line_quantity(line, 2, reason="Всё", author="И.", by=env["admin"])
    client.force_login(env["admin"])

    body = _report(client, customer)

    assert "Отменено" in body
    assert reverse("sale_line_cancel", args=[line.pk]) not in body


def test_the_confirmation_screen_asks_quantity_reason_and_author(client, env):
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    client.force_login(env["admin"])

    body = client.get(reverse("sale_line_cancel", args=[line.pk])).content.decode()

    assert 'name="quantity"' in body
    assert 'name="reason"' in body
    assert 'name="author"' in body
    assert 'max="4"' in body  # верхняя граница из остатка
    assert "PISTON CIRCLIP" in body


def test_the_screen_upper_bound_follows_earlier_reversals(client, env):
    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    cancel_sale_line_quantity(line, 1, reason="Одна", author="И.", by=env["admin"])
    client.force_login(env["admin"])

    body = client.get(reverse("sale_line_cancel", args=[line.pk])).content.decode()

    assert 'max="3"' in body


def test_cancelling_through_the_screen_returns_to_the_report(client, env):
    customer = Customer.objects.create(name="Саликов Рим Васильевич")
    sale = _sold(env, quantity="4", customer=customer)
    line = sale.lines.get()
    client.force_login(env["admin"])
    back = f"{reverse('reports_sales_by_client_detail')}?customer_id={customer.pk}"

    response = client.post(
        reverse("sale_line_cancel", args=[line.pk]),
        {"quantity": "1", "reason": "Клиент вернул", "author": "Иванов И.", "next": back},
        follow=True,
    )

    assert response.redirect_chain and back in response.redirect_chain[-1][0]
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("7")


def test_the_screen_refuses_more_than_remaining(client, env):
    sale = _sold(env, quantity="2")
    line = sale.lines.get()
    client.force_login(env["admin"])

    client.post(
        reverse("sale_line_cancel", args=[line.pk]),
        {"quantity": "5", "reason": "Много", "author": "И."},
    )

    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("8")  # склад не тронут
    assert StockReturn.objects.count() == 0


def test_a_seller_cannot_cancel_a_line(client, env, django_user_model):
    from apps.accounts import roles

    sale = _sold(env, quantity="4")
    line = sale.lines.get()
    seller = django_user_model.objects.create_user(username="prodavec", password=PASSWORD)
    seller.groups.add(Group.objects.get(name=roles.SELLER))
    client.login(username="prodavec", password=PASSWORD)

    denied = client.post(
        reverse("sale_line_cancel", args=[line.pk]),
        {"quantity": "1", "reason": "Хочу", "author": "Продавец"},
    )

    assert denied.status_code in (403, 302)
    env["cheap"].refresh_from_db()
    assert env["cheap"].quantity == Decimal("6")


# --- Оплата --------------------------------------------------------------------------------


def test_a_partial_cancellation_makes_a_paid_period_stale(env):
    from apps.customers.services import acknowledge_customer_period_payment
    from apps.reports.payment_status import payment_statuses_for_rows
    from apps.reports.services import resolve_period

    customer = Customer.objects.create(name="Саликов Рим Васильевич")
    sale = _sold(env, quantity="4", customer=customer)
    period = resolve_period({})
    acknowledge_customer_period_payment(customer_id=customer.pk, period=period, by=env["admin"])
    rows = [{"customer_id": customer.pk, "linked": True}]
    assert payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"] is True

    cancel_sale_line_quantity(
        sale.lines.get(), 1, reason="Одна", author="И.", by=env["admin"]
    )

    assert payment_statuses_for_rows(rows=rows, period=period)[customer.pk]["paid"] is False


@pytest.mark.postgresql
@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_postgresql_two_operators_cannot_reverse_the_same_unit(django_user_model):
    """Двое отменяют последнюю единицу одновременно: вернуться должна одна."""
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Нужен PostgreSQL через DENSTOCK_TEST_DATABASE_URL")

    admin = django_user_model.objects.create_superuser(username="parallel", password=PASSWORD)
    supplier = Supplier.objects.create(name="ООО Поставка")
    location = StorageLocation.objects.create(
        name="Ячейка", code="PP-01", storage_allowed=True, is_active=True
    )
    part = PartType.objects.create(
        name="ГАЙКА", category=Category.objects.create(name="Параллель"),
        unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("100"),
    )
    lot = create_stock_lot(
        _line(supplier, part, admin, quantity="5", unit_cost="100"), location, Decimal("5")
    )
    receive_stock_lot(lot, by=admin)
    sale = create_sale(customer_name="Иванов", by=admin)
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("100"), by=admin)
    complete_sale(sale, by=admin)
    line = sale.lines.get()
    lot.refresh_from_db()
    assert lot.quantity == Decimal("4")

    def attempt():
        close_old_connections()
        try:
            cancel_sale_line_quantity(line, 1, reason="Гонка", author="И.", by=admin)
            return "ok"
        except Exception as exc:  # noqa: BLE001 - интересен сам факт отказа
            return type(exc).__name__
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [f.result(timeout=30) for f in (pool.submit(attempt), pool.submit(attempt))]

    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")  # вернулась ровно одна единица
    assert results.count("ok") == 1
    assert reversible_quantity(line) == Decimal("0")
    assert StockMovement.objects.filter(
        movement_type=StockMovement.MovementType.RETURN_LOT
    ).count() == 1
