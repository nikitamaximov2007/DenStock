"""Быстрые действия: черновик корзины сохраняется на сервере сразу.

Ошибка со склада: сотрудник ввёл вес брутто/нетто, выбрал область применения,
клиента и количество, не нажал «Сохранить» и сразу нажал «Провести». Проведение
не видело введённого (поля принадлежали форме строки и уходили только кнопкой
«Сохранить количество»), сообщало «не хватает данных», а после ошибки страница
показывала старые значения: введённое терялось.

Теперь каждое поле сохраняется отдельным запросом (actions_cart_autosave) в
серверный черновик: количество - в строках документа, вес и область - в
сессии рядом с корзиной, клиент - туда же. «Провести» проводит сохранённый
черновик, а отказ ничего из него не стирает.
"""
import re
from decimal import Decimal

import pytest
from django.urls import reverse

from apps.actions.cart import cart_rows
from apps.actions.models import PartCustomsInfo, WarehouseAction
from apps.customers.models import Customer
from apps.inventory.models import StockLot, StockMovement
from apps.repairs.models import RepairOrder
from apps.sales.models import Sale
from tests.test_quick_action_customs_metadata import (  # noqa: F401
    PASSWORD,
    _stock,
    admin,
    env,
    make_user,
)

pytestmark = pytest.mark.django_db
WATERCRAFT = PartCustomsInfo.ApplicationArea.WATERCRAFT


@pytest.fixture
def boss(client, env):  # noqa: F811
    client.force_login(env["admin"])
    return client


def _row_key(env):  # noqa: F811
    return f"{env['part'].pk}:{env['loc'].pk}"


def _add(http, env, *, kind="sale", qty="1"):  # noqa: F811
    response = http.post(reverse("actions_cart_add"), {
        "part_id": env["part"].pk, "location_id": env["loc"].pk,
        "action_type": kind, "quantity": qty, "q": "700100",
    })
    assert response.status_code == 302
    model = Sale if kind == "sale" else RepairOrder
    return model.objects.filter(status=model.Status.DRAFT).latest("pk")


def _save(http, kind="sale", **fields):
    return http.post(reverse("actions_cart_autosave"), {"kind": kind, **fields})


def _quantity(http, env, value, kind="sale", **extra):  # noqa: F811
    return _save(http, kind, group="quantity", row_key=_row_key(env), quantity=value, **extra)


def _customs(http, env, kind="sale", **values):  # noqa: F811
    return _save(http, kind, group="customs", part_id=env["part"].pk, **values)


def _customer(http, customer_id, kind="sale"):
    return _save(http, kind, group="customer", customer_id=customer_id)


def _complete(http, kind="sale", **extra):
    payload = {"kind": kind, "q": "700100", **extra}
    return http.post(reverse("actions_cart_complete"), payload, follow=True)


def _page(http, kind="sale"):
    return http.get(reverse("actions_scan"), {"kind": kind}).content.decode()


def _messages(response):
    return [str(message) for message in response.context["messages"]]


def _input_value(html, name):
    match = re.search(rf'name="{name}"[^>]*?value="([^"]*)"', html, re.S)
    return match.group(1) if match else None


def _fill_customs(http, env, kind="sale"):  # noqa: F811
    response = _customs(
        http, env, kind,
        gross_weight_kg="0,14", net_weight_kg="0.13", application_area=WATERCRAFT,
    )
    assert response.status_code == 200, response.content


# --- 15. Сегодняшняя ошибка со склада, дословно ----------------------------------------


def test_todays_warehouse_failure_values_entered_without_save_are_completed(boss, env):  # noqa: F811
    cart = _add(boss, env)
    customer = Customer.objects.create(name="Иванов")
    # Сотрудник вводит поля по одному, кнопку «Сохранить» не нажимает.
    _fill_customs(boss, env)
    assert _customer(boss, customer.pk).status_code == 200
    assert _quantity(boss, env, "3").status_code == 200

    response = _complete(boss)  # в POST нет ни веса, ни количества

    assert not [m for m in _messages(response) if "не хватает данных" in m]
    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
    assert cart.customer == customer
    assert sum(line.quantity for line in cart.lines.all()) == Decimal("3")
    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    assert (customs.gross_weight_kg, customs.net_weight_kg) == (Decimal("0.14"), Decimal("0.13"))
    assert customs.application_area == WATERCRAFT
    assert WarehouseAction.objects.get().quantity == Decimal("3")


def test_a_missing_field_fails_keeps_everything_and_then_completes_without_reentry(boss, env):  # noqa: F811
    cart = _add(boss, env)
    customer = Customer.objects.create(name="Петров")
    _customs(boss, env, gross_weight_kg="0.14", net_weight_kg="0.13")  # области нет
    _customer(boss, customer.pk)
    _quantity(boss, env, "2")
    movements = StockMovement.objects.count()

    failed = _complete(boss)

    assert any("не выбрана область применения" in m for m in _messages(failed))
    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT
    assert StockMovement.objects.count() == movements  # склад не тронут
    assert not WarehouseAction.objects.exists()
    page = _page(boss)
    assert _input_value(page, "gross_weight_kg") == "0.14"
    assert _input_value(page, "net_weight_kg") == "0.13"
    assert _input_value(page, "quantity") == "2"
    assert re.search(rf'value="{customer.pk}"[^>]*selected', page)

    _customs(boss, env, application_area=WATERCRAFT)  # исправляет одно поле
    done = _complete(boss)

    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED, _messages(done)
    assert sum(line.quantity for line in cart.lines.all()) == Decimal("2")
    customs = PartCustomsInfo.objects.get(part_type=env["part"])
    assert customs.gross_weight_kg == Decimal("0.14")
    assert customs.application_area == WATERCRAFT


# --- A-G. Каждое поле сохраняется само и возвращается после перезагрузки -----------------


def test_quantity_autosaves_into_the_draft_document(boss, env):  # noqa: F811
    cart = _add(boss, env)
    response = _quantity(boss, env, "3")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert cart_rows(cart)[0].quantity == Decimal("3")
    assert _input_value(_page(boss), "quantity") == "3"  # перезагрузка


def test_customer_autosaves_and_completion_needs_no_customer_in_its_post(boss, env):  # noqa: F811
    cart = _add(boss, env)
    _fill_customs(boss, env)
    customer = Customer.objects.create(name="Сидоров", phone="+79990001122")

    assert _customer(boss, customer.pk).json()["ok"] is True
    assert re.search(rf'value="{customer.pk}"[^>]*selected', _page(boss))
    _complete(boss)

    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
    assert cart.customer == customer


@pytest.mark.parametrize(
    ("field", "raw", "shown"),
    [("gross_weight_kg", "0,14", "0.14"), ("net_weight_kg", "0.13", "0.13")],
)
def test_each_weight_autosaves_with_comma_or_dot(boss, env, field, raw, shown):  # noqa: F811
    _add(boss, env)
    assert _customs(boss, env, **{field: raw}).json()["ok"] is True
    assert _input_value(_page(boss), field) == shown
    assert not PartCustomsInfo.objects.filter(part_type=env["part"]).exists()  # до проведения


def test_application_area_autosaves(boss, env):  # noqa: F811
    _add(boss, env)
    assert _customs(boss, env, application_area=WATERCRAFT).json()["ok"] is True
    assert re.search(rf'value="{WATERCRAFT}"\s+selected', _page(boss))


def test_every_draft_field_on_the_screen_is_autosaved(boss, env):  # noqa: F811
    """Ни одно редактируемое поле корзины не зависит от кнопки «Сохранить»."""
    _add(boss, env)
    page = _page(boss)
    for name in ("quantity", "gross_weight_kg", "net_weight_kg", "application_area",
                 "customer_id"):
        tag = re.search(rf'<(?:input|select)[^>]*name="{name}"[^>]*>', page, re.S)
        assert tag and "data-autosave-group" in tag.group(0), name
    assert 'data-autosave-complete' in page
    assert "data-autosave-fallback" in page  # кнопка осталась только для страницы без скрипта
    assert "quick_actions_autosave.js" in page


def test_reload_restores_every_value_from_the_server(boss, env, client, make_user):  # noqa: F811
    _add(boss, env)
    customer = Customer.objects.create(name="Козлов")
    _quantity(boss, env, "4")
    _fill_customs(boss, env)
    _customer(boss, customer.pk)

    page = _page(boss)

    assert _input_value(page, "quantity") == "4"
    assert _input_value(page, "gross_weight_kg") == "0.14"
    assert _input_value(page, "net_weight_kg") == "0.13"
    assert re.search(rf'value="{WATERCRAFT}"\s+selected', page)
    assert re.search(rf'value="{customer.pk}"[^>]*selected', page)


# --- J/L. Отказ ничего не стирает; плохой ввод - понятный ответ, не 500 ------------------


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"gross_weight_kg": "abc"}, "числом"),
        ({"gross_weight_kg": "-1"}, "больше нуля"),
        ({"net_weight_kg": "0.0001"}, "3 знаков"),
        ({"net_weight_kg": "99999999"}, "слишком большой"),
        ({"application_area": "МОТОЦИКЛ"}, "из списка"),
    ],
)
def test_invalid_customs_input_is_a_controlled_rejection(boss, env, fields, message):  # noqa: F811
    _add(boss, env)
    _fill_customs(boss, env)
    response = _customs(boss, env, **fields)

    assert response.status_code == 400
    assert message in " ".join(response.json()["errors"].values())
    field = next(iter(fields))
    page = _page(boss)
    if field != "application_area":
        assert _input_value(page, field) == fields[field]  # видно, что именно не принято
    assert message in page
    assert any(message in m for m in _messages(_complete(boss, customer_id="")))
    assert Sale.objects.get().status == Sale.Status.DRAFT


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("abc", "Некорректное количество"),
        ("-2", "больше нуля"),
        ("0", "«Убрать»"),
        ("999", ""),  # больше остатка в ячейке
    ],
)
def test_invalid_quantity_is_rejected_kept_visible_and_blocks_completion(boss, env, raw, message):  # noqa: F811
    cart = _add(boss, env, qty="2")
    _fill_customs(boss, env)
    customer = Customer.objects.create(name="Иванов")
    _customer(boss, customer.pk)

    response = _quantity(boss, env, raw)

    assert response.status_code == 400
    assert message in " ".join(response.json()["errors"].values())
    assert cart_rows(cart)[0].quantity == Decimal("2")  # черновик не испорчен
    assert _input_value(_page(boss), "quantity") == raw
    result = _complete(boss)
    assert any(f"«{raw}»" in m and "Исправьте" in m for m in _messages(result))
    cart.refresh_from_db()
    assert cart.status == Sale.Status.DRAFT  # не проведено со старым количеством

    _quantity(boss, env, "3")
    _complete(boss)
    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
    assert sum(line.quantity for line in cart.lines.all()) == Decimal("3")


@pytest.mark.parametrize("raw", ["999999", "abc", "²", "-1"])
def test_an_unknown_customer_is_rejected_not_a_404_or_500(boss, env, raw):  # noqa: F811
    _add(boss, env)
    response = _customer(boss, raw)
    assert response.status_code == 400
    assert "не найдена" in " ".join(response.json()["errors"].values())

    result = _complete(boss, customer_id=raw)
    assert result.status_code == 200
    assert Sale.objects.get().status == Sale.Status.DRAFT


def test_completion_without_any_customer_is_a_business_error(boss, env):  # noqa: F811
    _add(boss, env)
    _fill_customs(boss, env)
    result = _complete(boss)
    assert result.status_code == 200
    assert "Выберите карточку клиента." in _messages(result)


def test_a_weight_pair_error_keeps_both_values_visible_and_unsaved(boss, env):  # noqa: F811
    _add(boss, env)
    response = _customs(boss, env, gross_weight_kg="0.1", net_weight_kg="0.13")
    assert response.status_code == 400
    page = _page(boss)
    assert _input_value(page, "gross_weight_kg") == "0.1"
    assert "не может быть меньше" in page

    assert _customs(boss, env, gross_weight_kg="0.14", net_weight_kg="0.13").status_code == 200
    page = _page(boss)
    assert "не может быть меньше" not in page
    assert _input_value(page, "gross_weight_kg") == "0.14"


def test_bad_requests_get_json_errors(boss, env):  # noqa: F811
    _add(boss, env)
    assert _save(boss, "sale", group="nope").status_code == 400
    assert _save(boss, "", group="quantity").status_code == 400
    assert _save(boss, "sale", group="quantity", row_key="x").status_code == 409
    assert _save(boss, "sale", group="customs", part_id="x").status_code == 409
    no_cart = _save(boss, "repair", group="customer", customer_id="1")
    assert no_cart.status_code == 409
    assert boss.get(reverse("actions_cart_autosave")).status_code == 405


# --- M. Успех закрывает черновик, ничего не переносится в следующий ---------------------


def test_success_closes_the_draft_and_the_next_cart_starts_clean(boss, env):  # noqa: F811
    _add(boss, env)
    _fill_customs(boss, env)
    customer = Customer.objects.create(name="Иванов")
    _customer(boss, customer.pk)
    _complete(boss)
    session = boss.session
    for key in ("actions_cart_customs", "actions_cart_customer", "actions_cart_rejected"):
        assert not [k for k in (session.get(key) or {}) if str(k).startswith("sale")], key

    _add(boss, env)  # новая корзина
    page = _page(boss)
    assert not re.search(rf'value="{customer.pk}"[^>]*selected', page)
    # Вес теперь запомнен в карточке детали: это прежнее поведение проведения.
    assert _input_value(page, "gross_weight_kg") == "0.140"


# --- N/O. Старый ответ не побеждает новый; автосохранение не трогает склад ----------------


def test_an_older_autosave_from_the_same_page_cannot_overwrite_a_newer_one(boss, env):  # noqa: F811
    cart = _add(boss, env)
    assert _quantity(boss, env, "3", client="page-1", rev="3").status_code == 200
    stale = _quantity(boss, env, "1", client="page-1", rev="2")

    assert stale.json() == {"ok": True, "stale": True}
    assert cart_rows(cart)[0].quantity == Decimal("3")
    # Новая страница (перезагрузка) начинает свою нумерацию и принимается.
    assert _quantity(boss, env, "2", client="page-2", rev="1").status_code == 200
    assert cart_rows(cart)[0].quantity == Decimal("2")


def test_rapid_edits_leave_the_last_value_and_never_touch_stock(boss, env):  # noqa: F811
    cart = _add(boss, env)
    lots = list(StockLot.objects.values_list("pk", "quantity"))
    movements = StockMovement.objects.count()
    rev = 0
    for step, value in enumerate(("1", "12", "123", "5"), start=1):
        rev += 1  # одна страница нумерует все свои сохранения подряд
        _quantity(boss, env, value, client="page", rev=str(rev))
        rev += 1
        _customs(boss, env, gross_weight_kg=f"0.{step}4", client="page", rev=str(rev))

    assert cart_rows(cart)[0].quantity == Decimal("5")  # 12 и 123 больше остатка: отклонены
    assert list(StockLot.objects.values_list("pk", "quantity")) == lots
    assert StockMovement.objects.count() == movements
    assert not WarehouseAction.objects.exists()
    assert Sale.objects.count() == 1
    assert cart.lines.count() == 1


def test_completion_happens_once_however_many_autosaves_preceded_it(boss, env):  # noqa: F811
    cart = _add(boss, env)
    _fill_customs(boss, env)
    customer = Customer.objects.create(name="Иванов")
    for _ in range(3):
        _customer(boss, customer.pk)
        _quantity(boss, env, "2")
    _complete(boss, request_token="tok-1")
    _complete(boss, request_token="tok-1")  # повтор той же формы

    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
    assert WarehouseAction.objects.count() == 1
    assert StockLot.objects.get().quantity == Decimal("8")


# --- 16. Ремонт: те же поля, цена по-прежнему из карточки детали --------------------------


def test_repair_autosaves_the_same_fields_and_keeps_the_catalog_price(boss, env):  # noqa: F811
    order = _add(boss, env, kind="repair")
    customer = Customer.objects.create(name="Мастер")
    _fill_customs(boss, env, kind="repair")
    _customer(boss, customer.pk, kind="repair")
    _quantity(boss, env, "2", kind="repair")
    page = _page(boss, "repair")
    assert 'name="customer_unit_price_rub"' not in page
    assert 'name="unit_price"' not in page

    _complete(boss, kind="repair")

    order.refresh_from_db()
    assert order.status == RepairOrder.Status.COMPLETED
    assert order.customer == customer
    line = order.lines.get()
    assert line.quantity == Decimal("2")
    assert line.customer_unit_price_rub == env["part"].recommended_price


def test_sale_and_repair_drafts_do_not_share_autosaved_values(boss, env):  # noqa: F811
    _add(boss, env, kind="sale")
    _add(boss, env, kind="repair")
    sale_customer = Customer.objects.create(name="Продажа")
    _customer(boss, sale_customer.pk, kind="sale")
    _customs(boss, env, "sale", gross_weight_kg="0.5")

    page = _page(boss, "repair")
    repair_select = page.split('id="customer-select-repair"')[1].split("</select>")[0]
    assert "selected" not in repair_select
    repair_panel = page.split('data-kind="repair"')[1]
    assert _input_value(repair_panel, "gross_weight_kg") == ""


# --- 9. Кнопка «Сохранить количество» осталась только запасным путём без скрипта ---------


def test_the_no_script_save_button_still_saves_and_clears_a_rejection(boss, env):  # noqa: F811
    cart = _add(boss, env)
    _quantity(boss, env, "abc")
    response = boss.post(reverse("actions_cart_update"), {
        "kind": "sale", "operation": "set", "row_key": _row_key(env), "quantity": "2",
        "gross_weight_kg": "0.14", "net_weight_kg": "0.13", "application_area": WATERCRAFT,
        "q": "700100",
    })
    assert response.status_code == 302
    assert cart_rows(cart)[0].quantity == Decimal("2")
    customer = Customer.objects.create(name="Иванов")
    _complete(boss, customer_id=customer.pk)
    cart.refresh_from_db()
    assert cart.status == Sale.Status.COMPLETED
