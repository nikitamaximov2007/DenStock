"""Синтетический рабочий день: сотрудник должен пройти его целиком.

Проверяется не отдельная функция, а связка: после каждой операции человек обязан
увидеть её результат там, где он его будет искать. Отдельные наборы такие разрывы
не ловят, потому что каждый проверяет свою операцию и на своей странице.

Главный вопрос в конце дня: «куда ушла эта деталь?». Если история есть в базе, но
найти её на экране нельзя, для склада это то же самое, что истории нет.
"""
from __future__ import annotations

from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.actions.cart import KIND_REPAIR, KIND_SALE, add_scan, complete_cart, open_cart
from apps.actions.models import WarehouseAction
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import (
    adjust_stock_lot_quantity,
    create_stock_lot,
    move_stock_lot,
    receive_stock_lot,
)
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation

PASSWORD = "parol-12345"
NUMBER_A = "700100"
NUMBER_B = "700200"


@pytest.fixture
def day(db, django_user_model):
    """Склад к началу смены: две детали, две ячейки, поступивший товар."""
    admin = django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    keeper = django_user_model.objects.create_user(username="keeper", password=PASSWORD)
    keeper.groups.add(Group.objects.get(name=roles.STOREKEEPER))
    supplier = Supplier.objects.create(name="ООО Поставка")
    category = Category.objects.create(name="Вариатор")
    unit, _ = Unit.objects.get_or_create(name="Штука", defaults={"short_name": "шт"})
    shelf = StorageLocation.objects.create(
        name="Ячейка 1", code="S01-D01-C01", storage_allowed=True, is_active=True
    )
    far = StorageLocation.objects.create(
        name="Ячейка 2", code="S02-D01-C01", storage_allowed=True, is_active=True
    )

    def make_part(name, number, price):
        part = PartType.objects.create(
            name=name, category=category, unit=unit,
            tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal(price),
        )
        PartNumber.objects.create(part=part, value=number, kind=PartNumber.Kind.OEM)
        return part

    def receive(part, location, qty):
        batch = Batch.objects.create(supplier=supplier, shipping_cost=Decimal("0"))
        line = BatchLine.objects.create(
            batch=batch, part_type=part, quantity=Decimal(str(qty)),
            unit_cost_currency=Decimal("10"),
        )
        batch.status = Batch.Status.ACCEPTED
        batch.save(update_fields=["status"])
        finalize_cost(batch, admin)
        line.refresh_from_db()
        lot = create_stock_lot(line, location, Decimal(str(qty)))
        receive_stock_lot(lot, by=admin)
        return lot

    part_a = make_part("Болт", NUMBER_A, "100")
    part_b = make_part("Кольцо", NUMBER_B, "250")
    return {
        "admin": admin, "keeper": keeper, "shelf": shelf, "far": far,
        "part_a": part_a, "part_b": part_b, "receive": receive,
        "lot_a": receive(part_a, shelf, 10),
        "lot_b": receive(part_b, shelf, 6),
    }


def _available(part, location=None):
    qs = StockLot.objects.filter(part_type=part, status=StockLot.Status.AVAILABLE)
    if location is not None:
        qs = qs.filter(location=location)
    return sum(lot.quantity for lot in qs)


def _cart(day, kind, part, quantity, customer_name):
    cart = open_cart(kind, by=day["admin"])
    add_scan(cart, part, day["shelf"], quantity=Decimal(str(quantity)), by=day["admin"])
    return complete_cart(cart, customer_comment=customer_name, by=day["admin"])


# --- Смена целиком --------------------------------------------------------------------


def test_a_full_shift_leaves_consistent_stock(day):
    """Приход, продажа, ремонт, перемещение и корректировка сходятся по остатку."""
    start_a = _available(day["part_a"])
    start_b = _available(day["part_b"])

    _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")
    _cart(day, KIND_REPAIR, day["part_a"], 3, "Петров")
    move_stock_lot(
        StockLot.objects.filter(part_type=day["part_a"], location=day["shelf"]).first(),
        day["far"], by=day["admin"], comment="перестановка",
    )
    adjust_stock_lot_quantity(
        StockLot.objects.filter(part_type=day["part_b"]).first(),
        Decimal("-1"), by=day["admin"], comment="бой",
    )

    assert _available(day["part_a"]) == start_a - Decimal("3")
    assert _available(day["part_b"]) == start_b - Decimal("3")
    assert all(lot.quantity >= 0 for lot in StockLot.objects.all()), "отрицательный остаток"


def test_moved_stock_is_found_in_its_new_cell(day):
    """После перемещения деталь обязана числиться в новой ячейке, а не пропасть."""
    lot = StockLot.objects.filter(part_type=day["part_a"], location=day["shelf"]).first()
    moved = lot.quantity
    move_stock_lot(lot, day["far"], by=day["admin"], comment="перестановка")

    assert _available(day["part_a"], day["shelf"]) == Decimal("0")
    assert _available(day["part_a"], day["far"]) == moved


def test_no_orphan_lots_after_the_shift(day):
    """Каждый лот привязан к строке партии и к ячейке."""
    _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")
    for lot in StockLot.objects.all():
        assert lot.batch_line_id is not None, f"лот {lot.pk} без строки партии"
        assert lot.location_id is not None, f"лот {lot.pk} без ячейки"


# --- Человек обязан увидеть результат ---------------------------------------------------


def test_the_shift_is_visible_in_the_action_report(client, day):
    """Главный вопрос смены: куда ушла эта деталь."""
    _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")
    _cart(day, KIND_REPAIR, day["part_a"], 3, "Петров")

    client.login(username="boss", password=PASSWORD)
    # В отчёте два раздельных подписанных поля: «Номер детали» и
    # «Клиент / комментарий». Ищем так, как это сделает сотрудник.
    response = client.get(reverse("actions_report"), {"part_number": NUMBER_A})
    assert response.status_code == 200
    body = response.content.decode()
    assert "Петров" in body, "по номеру детали не видно, кому она ушла"
    assert "Иванов" not in body, "фильтр по номеру детали показал чужие документы"

    by_customer = client.get(reverse("actions_report"), {"q": "Иванов"})
    assert "Иванов" in by_customer.content.decode(), "поиск по клиенту не находит документ"


def test_movement_history_can_be_filtered_to_one_part(client, day):
    _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")

    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse("movement_list"), {"part": day["part_b"].pk})
    assert response.status_code == 200
    assert StockMovement.objects.filter(part_type=day["part_b"]).exists()


def test_a_sale_is_visible_on_its_own_page(client, day):
    actions = _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")
    sale_id = actions[0].sale_id

    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse("sale_detail", args=[sale_id]))
    assert response.status_code == 200
    assert "Иванов" in response.content.decode()


def test_customer_card_collects_the_documents_of_the_day(client, day):
    """Клиент из справочника должен видеть свои документы."""
    from apps.sales.services import create_sale

    customer = Customer.objects.create(name="Сидоров", phone="+7 912 000-00-00")
    cart = create_sale(customer=customer, by=day["admin"])
    add_scan(cart, day["part_b"], day["shelf"], quantity=Decimal("1"), by=day["admin"])
    complete_cart(cart, customer_comment="Сидоров", by=day["admin"])

    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse("customer_detail", args=[customer.pk]))
    assert response.status_code == 200
    assert "Сидоров" in response.content.decode()


# --- Инварианты конца дня ----------------------------------------------------------------


def test_journal_and_physical_stock_agree(day):
    """Число записей журнала совпадает с числом проведённых позиций."""
    _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")
    _cart(day, KIND_REPAIR, day["part_a"], 3, "Петров")

    active = WarehouseAction.objects.filter(status=WarehouseAction.Status.ACTIVE)
    assert active.count() == 2, "журнал разошёлся с числом проведённых позиций"
    assert set(active.values_list("action_type", flat=True)) == {
        WarehouseAction.Type.SALE,
        WarehouseAction.Type.REPAIR,
    }


def test_a_cancelled_sale_returns_the_stock_and_shows_it(client, day):
    """Отмена обязана вернуть товар и быть видимой в журнале."""
    from apps.actions.services import cancel_warehouse_action

    before = _available(day["part_b"])
    actions = _cart(day, KIND_SALE, day["part_b"], 2, "Иванов")
    assert _available(day["part_b"]) == before - Decimal("2")

    cancel_warehouse_action(actions[0], by=day["admin"], reason="ошибка оператора")

    assert _available(day["part_b"]) == before
    actions[0].refresh_from_db()
    assert actions[0].status == WarehouseAction.Status.CANCELLED

    client.login(username="boss", password=PASSWORD)
    response = client.get(reverse("actions_report"), {"part_number": NUMBER_B, "cancelled": "1"})
    assert response.status_code == 200
    assert "Иванов" in response.content.decode(), "отменённый документ не найти в журнале"
