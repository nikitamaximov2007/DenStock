"""Незавершённая инвентаризация показывает ячейку такой, какая она сейчас.

Настоящий случай со склада: оператор начал пересчёт ячейки, отсканировал часть
деталей и отвлёкся. Остальное приняли в ту же ячейку обычной приёмкой. Экран
пересчёта продолжал показывать посчитанное раньше число и подписывал его
«Стоимость ячейки», хотя в ячейке лежало втрое больше.

Причина не в устаревшем снимке: снимка не было вовсе. Экран считал всё по
собственным строкам сканирования и склад не спрашивал. Здесь закреплено, что
он спрашивает, что посчитанное при этом остаётся нетронутым, и что провести
устаревший подсчёт поверх нового остатка нельзя.
"""
from decimal import Decimal

import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.counting.cell_state import cell_state, movements_touching_cell
from apps.counting.models import InventoryCountingSession
from apps.counting.services import CountingError, post_session, record_scan, start_session
from apps.inventory.models import StockLot, StockMovement
from apps.inventory.services import create_stock_lot, move_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.receipts.services import add_line, create_receipt, post_receipt
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location
from apps.writeoffs.services import quick_write_off

PASSWORD = "parol-12345"
ARTICLE = "01.1395.100"


@pytest.fixture
def admin(db, django_user_model):
    Group.objects.all()
    return django_user_model.objects.create_superuser(username="hozyain", password=PASSWORD)


@pytest.fixture
def env(db, admin):
    return {
        "admin": admin,
        "supplier": Supplier.objects.create(name="ООО Поставка"),
        "category": Category.objects.create(name="Пересчёт"),
        "cell": get_or_create_location("S07-D04-C01", name="Ячейка пересчёта"),
        "other": get_or_create_location("S07-D04-C02", name="Соседняя ячейка"),
    }


def _part(env, *, name="ПОРШЕНЬ", article=ARTICLE, price="1500"):
    part = PartType.objects.create(
        name=name, category=env["category"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK, recommended_price=Decimal(price),
    )
    PartNumber.objects.create(
        part=part, value=article, kind=PartNumber.Kind.ARTICLE, is_primary=True
    )
    return part


def _lot(env, part, quantity, unit_cost="100", location=None):
    batch = Batch.objects.create(supplier=env["supplier"], shipping_cost=Decimal("0"))
    line = BatchLine.objects.create(
        batch=batch, part_type=part,
        quantity=Decimal(quantity), unit_cost_currency=Decimal(unit_cost),
    )
    batch.status = Batch.Status.ACCEPTED
    batch.save(update_fields=["status"])
    finalize_cost(batch, env["admin"])
    line.refresh_from_db()
    lot = create_stock_lot(line, location or env["cell"], Decimal(quantity))
    receive_stock_lot(lot, by=env["admin"])
    return lot


def _receive(env, part, quantity, unit_cost="200", location=None):
    """Обычная приёмка сканером: своя партия, свои лоты и движения."""
    receipt = create_receipt(supplier=env["supplier"], comment="Приёмка", by=env["admin"])
    add_line(
        receipt, part_type=part, quantity=Decimal(quantity),
        unit_cost_rub=Decimal(unit_cost), location=location or env["cell"],
    )
    return post_receipt(receipt, by=env["admin"])


def _count(env, part, quantity="7"):
    """Начать пересчёт и отсканировать деталь нужное число раз."""
    session = start_session(location=env["cell"], by=env["admin"])
    for _ in range(int(quantity)):
        record_scan(session, ARTICLE, by=env["admin"])
    return session


def _state(env, session):
    return cell_state(env["cell"], since=session.created_at)


# --- A. Случай пользователя ----------------------------------------------------


def test_the_screen_shows_the_cell_as_it_is_now_not_as_it_was_counted(env):
    """7 посчитано, 23 приняли, в ячейке 30. Оба числа видны отдельно."""
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    assert session.counters()["total_quantity"] == Decimal("7")

    _receive(env, part, "23")

    state = _state(env, session)
    assert state["quantity"] == Decimal("30")  # склад знает правду
    assert session.counters()["total_quantity"] == Decimal("7")  # подсчёт не тронут
    assert state["changed"] is True
    assert state["changes_count"] >= 1


def test_the_page_shows_both_numbers_and_says_why_they_differ(client, env):
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    _receive(env, part, "23")
    client.force_login(env["admin"])

    body = client.get(reverse("counting_detail", args=[session.pk])).content.decode()

    assert "Посчитано в инвентаризации" in body
    assert "Сейчас в ячейке" in body
    assert "остатки в ячейке менялись" in body
    assert "Стоимость ячейки" not in body  # подпись, которая обещала не своё
    assert ">30<" in body.replace(" ", "").replace("\n", "")


def test_a_plain_refresh_keeps_showing_the_current_state(client, env):
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    _receive(env, part, "23")
    client.force_login(env["admin"])
    url = reverse("counting_detail", args=[session.pk])

    first = client.get(url).content.decode()
    second = client.get(url).content.decode()

    for body in (first, second):
        assert "Сейчас в ячейке" in body
    assert _state(env, session)["quantity"] == Decimal("30")


# --- C-E. Остальные складские операции ------------------------------------------


def test_a_transfer_into_the_cell_is_visible(env):
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    donor = _lot(env, part, "5", location=env["other"])

    move_stock_lot(donor, env["cell"], by=env["admin"])

    assert _state(env, session)["quantity"] == Decimal("12")


def test_a_transfer_out_of_the_cell_is_visible(env):
    part = _part(env)
    _lot(env, part, "20")
    leaving = _lot(env, part, "10")
    session = _count(env, part, "7")

    move_stock_lot(leaving, env["other"], by=env["admin"])

    assert _state(env, session)["quantity"] == Decimal("20")


def test_a_write_off_is_visible(env):
    part = _part(env)
    _lot(env, part, "25")
    session = _count(env, part, "7")

    quick_write_off(
        part=part, scanned_code=ARTICLE, reason="Брак",
        business_author="Иванов И.", quantity="2", by=env["admin"],
    )

    assert _state(env, session)["quantity"] == Decimal("23")


# --- G-H. Несколько лотов и себестоимость ---------------------------------------


def test_several_lots_of_one_part_add_up_and_keep_their_own_costs(env):
    part = _part(env)
    _lot(env, part, "4", unit_cost="100")
    _lot(env, part, "6", unit_cost="250")
    session = _count(env, part, "4")

    state = _state(env, session)
    assert state["quantity"] == Decimal("10")
    assert state["positions"] == 1  # одна деталь, два лота
    # 4*100 + 6*250: себестоимость каждого лота своя, средней не выдумываем.
    assert state["warehouse_cost"] == Decimal("1900")
    assert StockLot.objects.filter(location=env["cell"], part_type=part).count() == 2


def test_the_cell_cost_follows_the_receipt(env):
    part = _part(env)
    _lot(env, part, "7", unit_cost="100")
    session = _count(env, part, "7")
    before = _state(env, session)["warehouse_cost"]

    _receive(env, part, "23", unit_cost="200")

    after = _state(env, session)["warehouse_cost"]
    assert before == Decimal("700")
    assert after == Decimal("700") + Decimal("4600")


def test_a_zero_cost_lot_stays_zero(env):
    """Пересчёт заводит лоты с нулевой себестоимостью; клиентской ценой не подменяем."""
    part = _part(env, price="1500")
    _lot(env, part, "5", unit_cost="0")
    session = _count(env, part, "5")

    state = _state(env, session)
    assert state["quantity"] == Decimal("5")
    assert state["warehouse_cost"] == Decimal("0")  # а не 5 * 1500


# --- F. Экран ничего не создаёт --------------------------------------------------


def test_opening_the_page_creates_no_stock_records(client, env):
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    _receive(env, part, "23")
    client.force_login(env["admin"])
    before = (
        StockMovement.objects.count(), StockLot.objects.count(),
        InventoryCountingSession.objects.count(),
    )

    for _ in range(3):
        client.get(reverse("counting_detail", args=[session.pk]))
        client.get(reverse("counting_convert", args=[session.pk]))

    after = (
        StockMovement.objects.count(), StockLot.objects.count(),
        InventoryCountingSession.objects.count(),
    )
    assert before == after


# --- I-J. История и безопасность проведения --------------------------------------


def test_the_counted_result_is_never_rewritten_by_the_current_stock(env):
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    _receive(env, part, "23")

    line = session.lines.get()
    assert line.quantity_counted == Decimal("7")
    assert session.counters()["total_quantity"] == Decimal("7")
    assert _state(env, session)["quantity"] == Decimal("30")


def test_completion_is_refused_after_an_external_receipt(env):
    """Главная опасность: посчитанное прибавилось бы к уже принятому."""
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    _receive(env, part, "23")
    stock_before = _state(env, session)["quantity"]
    movements_before = StockMovement.objects.count()

    with pytest.raises(CountingError) as failure:
        post_session(session, by=env["admin"])

    assert "остатки в ячейке" in str(failure.value)
    assert "пересчитайте" in str(failure.value).lower()
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.DRAFT
    assert _state(env, session)["quantity"] == stock_before  # 30, а не 37
    assert StockMovement.objects.count() == movements_before
    assert session.lines.get().quantity_counted == Decimal("7")  # подсчёт цел


def test_the_screen_explains_the_refusal(client, env):
    part = _part(env)
    _lot(env, part, "7")
    session = _count(env, part, "7")
    _receive(env, part, "23")
    client.force_login(env["admin"])

    response = client.post(reverse("counting_post", args=[session.pk]), follow=True)

    body = response.content.decode()
    assert "остатки в ячейке" in body
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.DRAFT


def test_an_untouched_cell_still_completes_normally(env):
    """Обычный путь не сломан: ячейку не трогали - пересчёт проводится."""
    part = _part(env)
    session = _count(env, part, "3")

    post_session(session, by=env["admin"])

    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.POSTED
    assert session.inventory_number
    assert _state(env, session)["quantity"] == Decimal("3")


def test_only_movements_of_this_cell_block_completion(env):
    """Соседняя ячейка живёт своей жизнью и пересчёт не блокирует."""
    part = _part(env)
    session = _count(env, part, "3")
    other_part = _part(env, name="ДРУГАЯ", article="DRUGAYA-1")
    _lot(env, other_part, "5", location=env["other"])

    post_session(session, by=env["admin"])

    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.POSTED


def test_movements_are_counted_only_after_the_session_started(env):
    part = _part(env)
    _lot(env, part, "7")  # до начала сессии
    session = _count(env, part, "7")
    assert movements_touching_cell(env["cell"], session.created_at).count() == 0

    _receive(env, part, "23")

    assert movements_touching_cell(env["cell"], session.created_at).count() >= 1


def test_a_finished_session_is_not_told_to_recount(client, env):
    """Проведённую инвентаризацию звать на пересчёт незачем: она закрыта."""
    part = _part(env)
    session = _count(env, part, "3")
    post_session(session, by=env["admin"])
    _receive(env, part, "23")
    client.force_login(env["admin"])

    body = client.get(reverse("counting_detail", args=[session.pk])).content.decode()

    assert "Это завершённая инвентаризация" in body
    assert "пересчитайте ячейку заново" not in body
    assert "Сейчас в ячейке" in body  # текущее состояние всё равно показано
    session.refresh_from_db()
    assert session.status == InventoryCountingSession.Status.POSTED
