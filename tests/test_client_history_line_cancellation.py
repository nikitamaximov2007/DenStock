"""Отмена строки продажи прямо из истории клиента: частичная и полная.

Продуктовое правило простое, но у него две стороны. Частичная отмена
уменьшает то, что клиент получил: строка остаётся, количество и сумма
пересчитываются по действующему количеству, цена не трогается. Полная отмена
означает, что клиент не получил НИЧЕГО, и строка из обычной истории уходит.

Уходит только со ЭКРАНА. SaleLine, документ и возвраты остаются: количество в
строке по-прежнему историческое, отменённое доказывается каноническими
возвратами. Ноль в колонке «Кол-во» отвечал бы на вопрос «что мы давали
клиенту» словом «ничего» и занимал бы место наравне с настоящими выдачами.

Действующее количество считает ОДИН канонический слой (attach_line_reversals →
get_client_part_history), и им пользуются и экран, и итоги, и печать. Второго
калькулятора отмен в проекте нет и быть не должно.
"""
from decimal import Decimal
from urllib.parse import quote

import pytest
from django.urls import reverse

from apps.customers.models import Customer
from apps.inventory.models import StockMovement
from apps.reports.services import get_client_part_history, resolve_period
from apps.returns.models import StockReturn, StockReturnLine
from apps.sales.models import Sale, SaleLine
from apps.sales.services import reversible_quantity
from tests.test_client_history_operator_ux import (  # noqa: F401
    PASSWORD,
    _login,
    _lot,
    _sale,
    admin,
    data,
    make_user,
)

PRICE = Decimal("522")


@pytest.fixture
def scene(data, admin):  # noqa: F811 — фикстуры импортированы из соседнего модуля
    customer = Customer.objects.create(name="Иванов Пётр", phone="+79120000001")
    sale = _sale(data, customer=customer, items=(("bolt", 4),), price=PRICE)
    return {
        "data": data, "admin": admin, "customer": customer, "sale": sale,
        "line": SaleLine.objects.get(sale=sale),
    }


def _history(customer, **kw):
    return get_client_part_history(
        resolve_period({"preset": "all"}), customer_id=customer.pk, **kw
    )


def _timeline_qs(customer):
    return f"{reverse('reports_client_timeline')}?customer_id={customer.pk}&preset=all"


def _cancel(client, line, quantity, customer, *, follow=False):
    return client.post(
        reverse("sale_line_cancel", args=[line.pk]) + "?next=" + quote(_timeline_qs(customer)),
        {"quantity": str(quantity), "reason": "ошибка продавца", "author": "Пётр"},
        follow=follow,
    )


def _page(client, customer):
    return client.get(
        reverse("reports_client_timeline"), {"customer_id": customer.pk, "preset": "all"}
    ).content.decode()


def _print(client, customer):
    return client.get(
        reverse("reports_client_timeline_print"),
        {"customer_id": customer.pk, "preset": "all"},
    ).content.decode()


def _row_cells(html):
    """Ячейки количества истории (без заголовка таблицы)."""
    import re

    return [c.strip() for c in re.findall(r'num--qty[^>]*>\s*([^<]*)<', html) if c.strip()
            and c.strip() != "Кол-во"]


# --- 1. Выбор количества -----------------------------------------------------------------


def test_form_offers_one_to_four_for_a_line_of_four(client, scene):
    _login(client, scene["admin"])
    html = client.get(reverse("sale_line_cancel", args=[scene["line"].pk])).content.decode()
    assert 'name="quantity"' in html
    assert 'min="1"' in html and 'max="4"' in html and 'step="1"' in html


def test_max_follows_remaining_not_original(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    html = client.get(reverse("sale_line_cancel", args=[scene["line"].pk])).content.decode()
    assert 'max="3"' in html, "максимум обязан считаться от остатка, а не от исходных 4"


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "", "5", "2.5"])
def test_invalid_quantity_is_rejected_server_side(client, scene, bad):
    _login(client, scene["admin"])
    response = _cancel(client, scene["line"], bad, scene["customer"])
    assert response.status_code == 200, "недопустимое количество не должно проводиться"
    assert reversible_quantity(scene["line"]) == Decimal("4.000")


def test_more_than_remaining_is_rejected_after_a_partial(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    response = _cancel(client, scene["line"], 4, scene["customer"])
    assert response.status_code == 200
    assert reversible_quantity(scene["line"]) == Decimal("3.000")


# --- 2. Частичная отмена -----------------------------------------------------------------


def test_partial_cancellation_keeps_the_row_with_remaining_quantity(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    rows = _history(scene["customer"])
    assert len(rows) == 1
    row = rows[0]
    assert row["quantity"] == Decimal("3.000")
    assert row["unit_price"] == PRICE, "историческая цена не пересчитывается"
    assert row["amount"] == Decimal("1566.00"), "522 x 3"
    assert row["issued_quantity"] == Decimal("4.000"), "снимок выдачи остаётся"


def test_partial_cancellation_is_visible_on_the_screen(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    html = _page(client, scene["customer"])
    assert "3" in _row_cells(html)
    assert "1 566" in html and "2 088" not in html


def test_partial_cancellation_updates_the_client_total(client, scene):
    _login(client, scene["admin"])
    before = _page(client, scene["customer"])
    assert "2 088" in before
    _cancel(client, scene["line"], 1, scene["customer"])
    after = _page(client, scene["customer"])
    assert "1 566" in after


# --- 3. Полная отмена --------------------------------------------------------------------


def test_cancelling_everything_at_once_removes_the_row(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 4, scene["customer"])
    assert _history(scene["customer"]) == []
    assert _row_cells(_page(client, scene["customer"])) == []


def test_cancelling_the_remainder_removes_the_row(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    assert len(_history(scene["customer"])) == 1
    _cancel(client, scene["line"], 3, scene["customer"])
    assert _history(scene["customer"]) == []


def test_a_fully_cancelled_row_is_still_available_for_audit(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 4, scene["customer"])
    audit = _history(scene["customer"], include_fully_reversed=True)
    assert len(audit) == 1
    assert audit[0]["quantity"] == Decimal("0.000")
    assert audit[0]["issued_quantity"] == Decimal("4.000")


# --- 4. Аудит и склад --------------------------------------------------------------------


def test_history_documents_survive_full_cancellation(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 4, scene["customer"])
    line = SaleLine.objects.get(pk=scene["line"].pk)
    assert line.quantity == Decimal("4.000"), "исходное количество переписывать нельзя"
    assert Sale.objects.filter(pk=scene["sale"].pk).exists()
    assert StockReturn.objects.filter(status=StockReturn.Status.COMPLETED).exists()
    assert StockReturnLine.objects.filter(source_sale_line_id=line.pk).exists()


def test_stock_returns_exactly_the_cancelled_quantity(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    returned = sum(
        r.quantity for r in StockReturnLine.objects.filter(source_sale_line_id=scene["line"].pk)
    )
    assert returned == Decimal("1.000")


def test_repeated_submit_does_not_return_stock_twice(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 4, scene["customer"])
    moves_after_first = StockMovement.objects.count()
    returned_after_first = sum(
        r.quantity for r in StockReturnLine.objects.filter(source_sale_line_id=scene["line"].pk)
    )
    _cancel(client, scene["line"], 4, scene["customer"])
    assert StockMovement.objects.count() == moves_after_first, "второй submit двигал склад"
    returned_now = sum(
        r.quantity for r in StockReturnLine.objects.filter(source_sale_line_id=scene["line"].pk)
    )
    assert returned_now == returned_after_first == Decimal("4.000")


# --- 5. Печать использует тот же набор строк ---------------------------------------------


def test_print_shows_remaining_quantity_and_amount(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    html = _print(client, scene["customer"])
    assert "Болт" in html
    assert ">3<" in html.replace(" ", "")
    assert "1 566" in html
    assert "Итого с клиента" in html


def test_print_drops_a_fully_cancelled_row(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 4, scene["customer"])
    html = _print(client, scene["customer"])
    assert "Болт" not in html
    assert "0 ₽" in html or ">0<" in html.replace(" ", "")


def test_print_total_matches_the_visible_rows(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    assert "1 566" in _print(client, scene["customer"])


def test_print_keeps_the_russian_name(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    html = _print(client, scene["customer"])
    row = _history(scene["customer"])[0]
    assert row["russian_name"]
    assert row["russian_name"] in html


# --- 6. Пакет «Русское название» не сломан ------------------------------------------------


def test_russian_name_column_survives_a_partial_cancellation(client, scene):
    _login(client, scene["admin"])
    _cancel(client, scene["line"], 1, scene["customer"])
    html = _page(client, scene["customer"])
    assert "Русское название" in html
    assert 'name="russian_name"' in html, "инлайн-правка RU-имени осталась"
    assert reverse("reports_client_timeline_print") in html, "кнопка печати осталась"


# --- 7. Возврат на тот же экран с теми же фильтрами --------------------------------------


def test_redirect_returns_to_the_same_client_history(client, scene):
    _login(client, scene["admin"])
    response = _cancel(client, scene["line"], 1, scene["customer"])
    assert response.status_code == 302
    location = response["Location"]
    assert reverse("reports_client_timeline") in location
    assert f"customer_id={scene['customer'].pk}" in location
    assert "preset=all" in location
    assert client.get(location).status_code == 200


# --- 8. Права -----------------------------------------------------------------------------


def test_cancellation_requires_permission(client, scene, make_user):  # noqa: F811
    plain = make_user("prodavets")
    _login(client, plain)
    response = client.get(reverse("sale_line_cancel", args=[scene["line"].pk]))
    assert response.status_code in (403, 302)
    assert reversible_quantity(scene["line"]) == Decimal("4.000")


# --- 9. Предпросмотр ячейки возврата ------------------------------------------------------


def test_confirm_screen_shows_the_exact_return_cell(client, scene):
    """Экран обязан назвать ячейку, а не описать её словами."""
    _login(client, scene["admin"])
    html = client.get(reverse("sale_line_cancel", args=[scene["line"].pk])).content.decode()
    expected = scene["data"]["location"].short_code
    assert "Куда вернётся товар" in html
    assert expected in html, f"ячейка {expected} на экране подтверждения не названа"


def test_preview_cell_is_where_the_stock_actually_returns(client, scene):
    """Предпросмотр и проведение считает одна функция, и это проверяемо."""
    from apps.sales.services import sale_line_source_location

    _login(client, scene["admin"])
    html = client.get(reverse("sale_line_cancel", args=[scene["line"].pk])).content.decode()
    promised = sale_line_source_location(scene["line"])
    assert promised.short_code in html

    _cancel(client, scene["line"], 1, scene["customer"])
    moved = StockMovement.objects.filter(
        movement_type__in=(
            StockMovement.MovementType.RETURN_LOT,
            StockMovement.MovementType.RETURN_ITEM,
        )
    ).order_by("-pk").first()
    assert moved is not None, "возвратного движения не было"
    assert moved.to_location_id == promised.pk, "товар уехал не в обещанную ячейку"
    assert moved.quantity == Decimal("1.000")


def test_browser_cannot_choose_the_destination(client, scene):
    """Ячейку выбирает сервер: в форме её нет и подменить её нечем."""
    _login(client, scene["admin"])
    html = client.get(reverse("sale_line_cancel", args=[scene["line"].pk])).content.decode()
    form = html.split("<form", 1)[1].split("</form>", 1)[0]
    assert "location" not in form, "форма отдаёт выбор ячейки браузеру"

    other = scene["data"]["location"]
    response = client.post(
        reverse("sale_line_cancel", args=[scene["line"].pk]),
        {"quantity": "1", "reason": "подмена", "author": "QA",
         "location_id": "999999", "to_location": "999999"},
    )
    assert response.status_code in (302, 200)
    moved = StockMovement.objects.filter(
        movement_type__in=(
            StockMovement.MovementType.RETURN_LOT,
            StockMovement.MovementType.RETURN_ITEM,
        )
    ).order_by("-pk").first()
    assert moved is not None
    assert moved.to_location_id == other.pk, "склад послушал браузер, а не сервер"
