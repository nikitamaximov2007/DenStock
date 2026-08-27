"""Доступное количество для списания считается и без единой брони.

Экран быстрого списания падал с ошибкой на любой детали, у которой есть
остаток: помощник по броням возвращает только те лоты, на которых бронь
действительно висит, а расчёт обращался к нему по ключу для каждого лота.
У обычного лота брони нет - и это норма, а не исключение.

Проверка маршрута кодом 200 этого не ловила: без артикула деталь не ищется,
и падение начиналось ровно там, где оператор вводит номер.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.sales.services import (
    activate_reservation,
    add_stock_lot_to_reservation,
    create_reservation,
)
from apps.suppliers.models import Supplier
from apps.warehouse.models import StorageLocation
from apps.writeoffs.models import WriteOffDocument
from apps.writeoffs.services import (
    WriteOffError,
    available_quantity,
    quick_write_off,
)

PASSWORD = "parol-12345"


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            return django_user_model.objects.create_superuser(
                username=username, password=PASSWORD
            )
        user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def admin(make_user):
    return make_user("admin", is_superuser=True)


@pytest.fixture
def env(db, admin):
    return {
        "sup": Supplier.objects.create(name="ООО Поставка"),
        "cat": Category.objects.create(name="Аналоги"),
        "cell": StorageLocation.objects.create(
            name="Ячейка", code="S02-D01-C01", storage_allowed=True, is_active=True
        ),
        "admin": admin,
    }


def _part(env, *, name="ПОРШЕНЬ", article="01.1395.100"):
    part = PartType.objects.create(
        name=name, category=env["cat"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal("1000"),
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


def _lot(env, part, quantity="5", unit_cost="100"):
    batch = Batch.objects.create(supplier=env["sup"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, env["cell"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])
    return lot


# --- Сам расчёт --------------------------------------------------------------


def test_availability_works_without_any_reservation(env):
    """Тот самый случай, который ронял экран."""
    part = _part(env)
    _lot(env, part, quantity="5")
    assert available_quantity(part) == Decimal("5")


def _reserve(env, lot, quantity):
    """Активная бронь: черновик остаток ещё не держит."""
    reservation = create_reservation(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_reservation(reservation, lot, Decimal(quantity), by=env["admin"])
    return activate_reservation(reservation, by=env["admin"])


def test_availability_subtracts_an_active_reservation(env):
    part = _part(env)
    lot = _lot(env, part, quantity="5")
    _reserve(env, lot, "2")
    assert available_quantity(part) == Decimal("3")


def test_a_draft_reservation_does_not_hold_stock(env):
    part = _part(env)
    lot = _lot(env, part, quantity="5")
    reservation = create_reservation(customer_name="Иванов", by=env["admin"])
    add_stock_lot_to_reservation(reservation, lot, Decimal("2"), by=env["admin"])
    assert available_quantity(part) == Decimal("5")  # черновик ничего не держит


def test_availability_mixes_reserved_and_free_lots(env):
    """Часть лотов забронирована, часть нет - раньше падало на свободном."""
    part = _part(env)
    first = _lot(env, part, quantity="4")
    _lot(env, part, quantity="6")
    _reserve(env, first, "1")
    assert available_quantity(part) == Decimal("9")


def test_availability_of_a_part_without_stock_is_zero(env):
    assert available_quantity(_part(env)) == Decimal("0")


# --- Экран -------------------------------------------------------------------


def test_the_quick_write_off_screen_survives_a_scanned_article(client, env):
    """Маршрут без артикула отвечал 200 и раньше - проверяем с артикулом."""
    part = _part(env)
    _lot(env, part, quantity="5")
    client.force_login(env["admin"])

    response = client.get(reverse("write_off_quick") + "?q=01.1395.100")
    assert response.status_code == 200
    body = response.content.decode()
    assert "ПОРШЕНЬ" in body
    assert "5" in body


def test_the_screen_survives_a_part_with_a_reservation(client, env):
    part = _part(env)
    lot = _lot(env, part, quantity="5")
    _reserve(env, lot, "2")
    client.force_login(env["admin"])
    assert client.get(reverse("write_off_quick") + "?q=01.1395.100").status_code == 200


# --- Настоящее списание, а не код ответа -------------------------------------


def test_a_real_write_off_moves_stock_and_records_why(env):
    part = _part(env)
    lot = _lot(env, part, quantity="5", unit_cost="100")
    movements_before = StockMovement.objects.count()

    document = quick_write_off(
        part=part, scanned_code="01.1395.100", reason="Разбит при разгрузке",
        business_author="Иванов И.", by=env["admin"],
    )

    assert document.status == WriteOffDocument.Status.COMPLETED
    assert document.business_author == "Иванов И."
    # Оператор пишет причину словами; в перечне это «Прочее», а сам текст
    # сохраняется - иначе из документа не понять, за что списали.
    assert document.comment == "Разбит при разгрузке"
    assert document.reason == WriteOffDocument.Reason.OTHER
    lot.refresh_from_db()
    assert lot.quantity == Decimal("4")  # ровно одна единица
    assert available_quantity(part) == Decimal("4")
    assert StockMovement.objects.count() == movements_before + 1
    movement = StockMovement.objects.order_by("-pk").first()
    assert movement.part_type_id == part.pk
    assert movement.stock_lot_id == lot.pk
    assert movement.quantity == Decimal("1")
    assert movement.unit_cost_rub == Decimal("100.00")  # себестоимость лота


def test_cancelling_a_write_off_returns_the_unit_exactly_once(env):
    """Отмена возвращает ровно списанное; вторая отмена ничего не добавляет."""
    from apps.writeoffs.services import cancel_write_off

    part = _part(env)
    lot = _lot(env, part, quantity="5")
    document = quick_write_off(
        part=part, scanned_code="01.1395.100", reason="Брак",
        business_author="Иванов И.", by=env["admin"],
    )
    lot.refresh_from_db()
    assert lot.quantity == Decimal("4")

    cancel_write_off(document, by=env["admin"])
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")  # единица вернулась в свой лот
    assert lot.location_id == env["cell"].pk  # и в свою ячейку
    assert available_quantity(part) == Decimal("5")

    cancel_write_off(document, by=env["admin"])
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")  # вторая отмена не удваивает возврат
    document.refresh_from_db()
    assert document.status == WriteOffDocument.Status.CANCELED


def test_cancelling_a_draft_is_idempotent(env):
    """Черновик отменяется, повторная отмена ничего не ломает."""
    from apps.writeoffs.services import cancel_write_off, create_write_off

    draft = create_write_off(
        reason=WriteOffDocument.Reason.DAMAGED, comment="Черновик", by=env["admin"]
    )
    first = cancel_write_off(draft, by=env["admin"])
    second = cancel_write_off(draft, by=env["admin"])
    assert first.status == second.status == WriteOffDocument.Status.CANCELED


def test_a_write_off_never_takes_a_reserved_unit(env):
    """Бронь клиента списанием не забирается."""
    part = _part(env)
    lot = _lot(env, part, quantity="2")
    _reserve(env, lot, "2")
    assert available_quantity(part) == Decimal("0")

    with pytest.raises(WriteOffError):
        quick_write_off(
            part=part, scanned_code="01.1395.100", reason="Брак",
            business_author="Иванов И.", by=env["admin"],
        )
    lot.refresh_from_db()
    assert lot.quantity == Decimal("2")  # склад не тронут


def test_a_seller_cannot_write_off(client, env, make_user):
    part = _part(env)
    _lot(env, part, quantity="5")
    make_user("prodavec", role=roles.SELLER)
    client.login(username="prodavec", password=PASSWORD)
    response = client.get(reverse("write_off_quick") + "?q=01.1395.100")
    assert response.status_code in (403, 302)
    assert Customer.objects.count() == 0  # ничего не создалось попутно
