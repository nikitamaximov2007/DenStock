"""Незаполненная цена не становится нулём в продаже.

У детали без цены в карточке быстрая продажа раньше подставляла 0.00: деталь
уходила с прилавка бесплатно, а в отчёте это выглядело обычной продажей. Ноль
имеет право быть только тогда, когда его действительно проставили в карточке.

Правило касается только продажи. У ремонта цена клиента необязательна по
своей природе и показывается прочерком - там ничего не меняется. Ручной
документ продажи тоже не трогаем: там цену вводит человек, и его ноль
осознанный.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.actions.cart import add_scan, cart_rows, complete_cart, open_cart, set_row_quantity
from apps.actions.models import WarehouseAction
from apps.actions.services import ActionError, perform_action
from apps.actions.views import CART_SESSION_KEYS
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.customers.models import Customer
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale, SaleLine
from apps.sales.services import add_stock_lot_to_sale, complete_sale, create_sale
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location

PASSWORD = "parol-12345"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


@pytest.fixture
def env(db, admin):
    return {
        "admin": admin,
        "supplier": Supplier.objects.create(name="ООО Поставка"),
        "category": Category.objects.create(name="Цены"),
        "cell": get_or_create_location("S06-D01-C01", name="Ячейка"),
    }


def _part(env, *, name="ПОРШЕНЬ", article="01.1395.100", price="13100"):
    part = PartType.objects.create(
        name=name, category=env["category"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=None if price is None else Decimal(price),
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


def _stock(env, part, quantity="5", unit_cost="100"):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
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


def _draft(client, env, kind, part, quantity="1"):
    cart = open_cart(kind, by=env["admin"])
    add_scan(cart, part, env["cell"], quantity=Decimal(quantity), by=env["admin"])
    session = client.session
    session[CART_SESSION_KEYS[kind]] = cart.pk
    session.save()
    return cart


# --- A. Детали без цены нет места в продаже -----------------------------------


def test_a_part_without_a_price_cannot_be_added_to_a_sale(env):
    part = _part(env, name="БЕЗ ЦЕНЫ", article="BEZ-1", price=None)
    _stock(env, part)
    cart = open_cart("sale", by=env["admin"])

    with pytest.raises(ActionError) as failure:
        add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])

    assert "цена не задана" in str(failure.value)
    assert "БЕЗ ЦЕНЫ" in str(failure.value)
    assert SaleLine.objects.count() == 0  # ни строки, ни подставленного нуля
    assert cart_rows(cart) == []


def test_the_screen_says_why_and_leaves_no_empty_draft(client, env):
    part = _part(env, name="БЕЗ ЦЕНЫ", article="BEZ-1", price=None)
    lot = _stock(env, part)
    client.force_login(env["admin"])

    response = client.post(
        reverse("actions_cart_add"),
        {"action_type": "sale", "part_id": part.pk, "location_id": lot.location_id,
         "quantity": "1", "q": "BEZ-1"},
        follow=True,
    )

    body = response.content.decode()
    assert "цена не задана" in body
    assert "Укажите цену в карточке детали" in body
    assert SaleLine.objects.count() == 0
    assert Sale.objects.filter(status=Sale.Status.DRAFT).count() == 0


def test_the_single_scan_action_refuses_the_same_part(env):
    """Второй вход в продажу - действие без черновика - закрыт тем же правилом."""
    part = _part(env, name="БЕЗ ЦЕНЫ", article="BEZ-1", price=None)
    lot = _stock(env, part)
    before = (StockLot.objects.get(pk=lot.pk).quantity, StockMovement.objects.count())

    with pytest.raises(ActionError) as failure:
        perform_action(
            part=part, location=env["cell"], action_type=WarehouseAction.Type.SALE,
            quantity=Decimal("1"), customer_comment="Иванов", by=env["admin"],
        )

    assert "цена не задана" in str(failure.value)
    assert Sale.objects.count() == 0
    assert WarehouseAction.objects.count() == 0
    lot.refresh_from_db()
    assert (lot.quantity, StockMovement.objects.count()) == before  # склад не тронут


# --- B. Явный ноль остаётся законным ------------------------------------------


def test_an_explicit_zero_price_is_still_allowed(env):
    """Ноль, проставленный в карточке, - осознанное решение, а не пропуск."""
    part = _part(env, name="ПОДАРОК", article="ZERO-1", price="0")
    _stock(env, part)
    cart = open_cart("sale", by=env["admin"])

    row = add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])

    assert row.unit_price == Decimal("0")
    assert SaleLine.objects.count() == 1
    complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])
    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED


# --- C. Обычная деталь: цена и сумма ------------------------------------------


def test_a_priced_part_shows_the_price_and_the_line_total(client, env):
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])
    cart = _draft(client, env, "sale", part, quantity="2")

    row = cart_rows(cart)[0]
    assert row.unit_price == Decimal("13100")
    assert row.total_price == Decimal("26200")

    body = client.get(reverse("actions_scan"), {"kind": "sale"}).content.decode()
    assert "13 100 ₽" in body.replace(" ", " ")
    assert "26 200 ₽" in body.replace(" ", " ")
    assert '<th class="num--money">Цена</th>' in body
    assert '<th class="num--money">Сумма</th>' in body


# --- D. Цену задали позже -----------------------------------------------------


def test_the_part_can_be_sold_once_the_price_is_filled_in(env):
    part = _part(env, name="ПОЗЖЕ", article="POZZHE-1", price=None)
    _stock(env, part)
    cart = open_cart("sale", by=env["admin"])
    with pytest.raises(ActionError):
        add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])

    part.recommended_price = Decimal("13100")
    part.save(update_fields=["recommended_price"])

    row = add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])
    assert row.unit_price == Decimal("13100")

    complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])
    line = SaleLine.objects.get()
    assert line.unit_price == Decimal("13100")

    # Снимок документа не следует за карточкой: цена продажи историческая.
    part.recommended_price = Decimal("20000")
    part.save(update_fields=["recommended_price"])
    line.refresh_from_db()
    assert line.unit_price == Decimal("13100")


# --- E. Проведение нельзя обойти ----------------------------------------------


def _legacy_zero_draft(env, part, lot):
    """Черновик, каким его оставляла прежняя версия: ноль вместо пустой цены."""
    cart = create_sale(customer_name="Черновик сканера", comment="Корзина", by=env["admin"])
    add_stock_lot_to_sale(cart, lot, Decimal("1"), unit_price=Decimal("0"), by=env["admin"])
    return cart


def test_an_old_draft_with_an_invented_zero_cannot_be_completed(env):
    part = _part(env, name="СТАРЫЙ ЧЕРНОВИК", article="STARY-1", price=None)
    lot = _stock(env, part)
    cart = _legacy_zero_draft(env, part, lot)
    assert cart_rows(cart)[0].unit_price == Decimal("0")

    with pytest.raises(ActionError) as failure:
        complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])

    assert "цена не задана" in str(failure.value)
    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT  # проведения не случилось
    lot.refresh_from_db()
    assert lot.quantity == Decimal("5")  # склад не тронут


def test_an_old_zero_row_is_blocked_even_after_the_price_appears(env):
    """Ноль в строке не станет правдой оттого, что в карточке появилась цена."""
    part = _part(env, name="СТАРАЯ СТРОКА", article="STARAYA-1", price=None)
    lot = _stock(env, part)
    cart = _legacy_zero_draft(env, part, lot)
    part.recommended_price = Decimal("13100")
    part.save(update_fields=["recommended_price"])

    with pytest.raises(ActionError) as failure:
        complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])

    assert "Уберите её и добавьте деталь заново" in str(failure.value)
    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT


def test_a_bad_row_can_still_be_removed_from_the_draft(env):
    """Тупика быть не должно: испорченную строку оператор убирает как обычно."""
    part = _part(env, name="УБРАТЬ", article="UBRAT-1", price=None)
    lot = _stock(env, part)
    cart = _legacy_zero_draft(env, part, lot)

    set_row_quantity(cart, part, env["cell"], Decimal("0"), by=env["admin"])

    assert cart_rows(cart) == []
    assert SaleLine.objects.count() == 0


# --- F. История неприкосновенна ------------------------------------------------


def test_a_completed_sale_with_a_zero_line_is_left_alone(env):
    part = _part(env, name="ИСТОРИЯ", article="ISTORIYA-1", price=None)
    lot = _stock(env, part)
    sale = create_sale(customer_name="Иванов", comment="Старая продажа", by=env["admin"])
    add_stock_lot_to_sale(sale, lot, Decimal("1"), unit_price=Decimal("0"), by=env["admin"])
    sale = complete_sale(sale, by=env["admin"])

    line = SaleLine.objects.get()
    assert sale.status == Sale.Status.COMPLETED
    assert line.unit_price == Decimal("0")  # переписывать историю нельзя

    part.recommended_price = Decimal("13100")
    part.save(update_fields=["recommended_price"])
    line.refresh_from_db()
    assert line.unit_price == Decimal("0")


# --- G. Ремонт и клиенты не задеты ---------------------------------------------


def test_a_part_without_a_price_still_goes_into_a_repair(env):
    """У ремонта цена клиента необязательна: правило продажи туда не лезет."""
    part = _part(env, name="В РЕМОНТ", article="REMONT-1", price=None)
    _stock(env, part)
    cart = open_cart("repair", by=env["admin"])

    row = add_scan(cart, part, env["cell"], quantity=Decimal("1"), by=env["admin"])

    assert row.unit_price is None
    complete_cart(cart, customer=Customer.objects.create(name="Иванов"), by=env["admin"])
    cart.refresh_from_db()
    assert cart.status == RepairOrder.Status.COMPLETED
    assert cart.lines.get().customer_unit_price_rub is None  # прочерк, а не ноль


def test_the_customer_workflow_survives(client, env):
    Customer.objects.create(name="Саликов Рим Васильевич")
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])
    _draft(client, env, "sale", part, quantity="2")

    body = client.get(reverse("actions_scan"), {"kind": "sale"}).content.decode()
    assert 'name="customer_id"' in body
    assert "Саликов Рим Васильевич" in body
    assert "Создать клиента" in body
    assert 'name="customer_name"' not in body


def test_a_priced_sale_completes_through_the_screen(client, env):
    part = _part(env)
    lot = _stock(env, part)
    customer = Customer.objects.create(name="Иванов")
    client.force_login(env["admin"])
    cart = _draft(client, env, "sale", part, quantity="2")

    complete_cart(cart, customer=customer, by=env["admin"])

    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
    lot.refresh_from_db()
    assert lot.quantity == Decimal("3")  # две единицы ушли клиенту
    assert SaleLine.objects.get().unit_price == Decimal("13100")


# --- H. Поиск ведёт туда же ------------------------------------------------------


def test_search_still_routes_a_priced_part_into_the_sale(client, env):
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])

    body = client.get(reverse("part_search"), {"q": "01.1395.100"}).content.decode()

    assert "Продать" in body
    assert "kind=sale" in body
    assert "13 100" in body.replace(" ", " ")


def test_search_shows_a_dash_for_a_part_without_a_price(client, env):
    part = _part(env, name="БЕЗ ЦЕНЫ", article="BEZ-1", price=None)
    _stock(env, part)
    client.force_login(env["admin"])

    body = client.get(reverse("part_search"), {"q": "BEZ-1"}).content.decode()

    assert "БЕЗ ЦЕНЫ" in body
    assert "Цена: —" in body
    assert "Цена: 0" not in body
    card = client.get(reverse("part_detail", args=[part.pk])).content.decode()
    assert "—" in card
