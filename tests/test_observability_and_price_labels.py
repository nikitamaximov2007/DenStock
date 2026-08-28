"""След от ошибки 500 и одно название цены на операторских экранах.

Раньше падение при DEBUG=False не оставляло в бою ничего: консольный обработчик
Django отфильтрован по DEBUG, писем администраторам нет. Здесь закреплено, что
traceback доходит до потока ошибок вместе с методом, путём, пользователем и
сквозным номером запроса, и что секреты в запись не попадают.

Вторая половина файла про цену: за единицу она называется «Цена» и в поиске, и
на карточке, и в черновиках продажи и ремонта; «Сумма» остаётся итогом строки и
ценой не притворяется.
"""
import io
import logging
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.http import HttpResponse
from django.test import override_settings
from django.urls import path, reverse

from apps.actions.cart import add_scan, cart_rows, open_cart
from apps.actions.views import CART_SESSION_KEYS
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.core.observability import (
    REQUEST_ID_HEADER,
    RedactingFormatter,
    RequestContextFilter,
    redact,
)
from apps.customers.models import Customer
from apps.inventory.services import create_stock_lot, receive_stock_lot
from apps.procurement.models import Batch, BatchLine
from apps.procurement.services import finalize_cost
from apps.suppliers.models import Supplier
from apps.warehouse.addresses import get_or_create_location

PASSWORD = "parol-12345"


# --- Подопытный маршрут: только для тестов, в боевой urlconf его нет ----------


def boom(request):
    raise ValueError("сломалось, password=hunter2 и token=abc123")


def fine(request):
    return HttpResponse("ok")


urlpatterns = [
    path("boom/", boom, name="boom"),
    path("fine/", fine, name="fine"),
]


@pytest.fixture
def captured_errors():
    """Слушаем django.request тем же форматом, что настроен в приложении."""
    logger = logging.getLogger("django.request")
    configured = next(
        (h for h in logger.handlers if isinstance(h.formatter, RedactingFormatter)), None
    )
    assert configured is not None, "боевой обработчик django.request не настроен"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(configured.formatter)
    handler.setLevel(logging.ERROR)
    handler.addFilter(RequestContextFilter())
    logger.addHandler(handler)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)


# --- Настройка логирования ----------------------------------------------------


def test_the_error_logger_is_wired_for_production():
    logger = logging.getLogger("django.request")
    assert logger.level == logging.ERROR
    assert not logger.propagate  # иначе та же ошибка удвоится в журнале
    handler = next(h for h in logger.handlers if isinstance(h.formatter, RedactingFormatter))
    assert handler.level == logging.ERROR
    fmt = settings.LOGGING["formatters"]["operational"]["format"]
    for field in ("asctime", "levelname", "name", "request_id", "method", "path",
                  "user", "process", "threadName", "message"):
        assert f"%({field})" in fmt  # число или строка - важно, что поле есть


@override_settings(ROOT_URLCONF=__name__)
def test_an_uncaught_error_leaves_a_traceback(client, captured_errors, settings):
    assert settings.DEBUG is False  # именно в этом режиме следа и не было
    client.raise_request_exception = False

    response = client.get("/boom/", HTTP_AUTHORIZATION="Bearer sekret-value")

    assert response.status_code == 500
    written = captured_errors.getvalue()
    assert "Traceback (most recent call last)" in written
    assert "ValueError" in written
    assert "ERROR" in written
    assert "django.request" in written
    assert "GET /boom/" in written
    assert "user=anonymous" in written
    assert "pid=" in written and "thread=" in written


@override_settings(ROOT_URLCONF=__name__)
def test_the_traceback_names_the_operator(client, captured_errors, db, django_user_model):
    user = django_user_model.objects.create_user(username="kladovshik", password=PASSWORD)
    client.force_login(user)
    client.raise_request_exception = False

    client.get("/boom/")

    written = captured_errors.getvalue()
    assert f"user={user.pk}:kladovshik" in written


@override_settings(ROOT_URLCONF=__name__)
def test_secrets_never_reach_the_log(client, captured_errors):
    client.raise_request_exception = False

    client.post(
        "/boom/",
        {"password": "hunter2", "csrfmiddlewaretoken": "tok3n"},
        HTTP_AUTHORIZATION="Bearer sekret-value",
        HTTP_COOKIE="sessionid=abc; csrftoken=def",
    )

    written = captured_errors.getvalue()
    assert "hunter2" not in written
    assert "abc123" not in written
    assert "sekret-value" not in written
    assert "tok3n" not in written
    assert "password" in written  # имя поля остаётся, значение убрано
    assert "[скрыто]" in written


def test_redaction_keeps_the_field_names():
    text = 'password=hunter2 token: abc123 "authorization": "Bearer xyz" Cookie=sessionid=zzz'
    hidden = redact(text)
    assert "hunter2" not in hidden
    assert "abc123" not in hidden
    assert "xyz" not in hidden
    assert "password" in hidden and "token" in hidden


@override_settings(ROOT_URLCONF=__name__)
def test_a_healthy_request_carries_its_own_number(client):
    response = client.get("/fine/")
    assert response.status_code == 200
    assert response[REQUEST_ID_HEADER]
    other = client.get("/fine/")
    assert other[REQUEST_ID_HEADER] != response[REQUEST_ID_HEADER]


@override_settings(ROOT_URLCONF=__name__)
def test_the_failed_request_is_findable_by_its_number(client, captured_errors):
    client.raise_request_exception = False
    response = client.get("/boom/")
    assert response[REQUEST_ID_HEADER] in captured_errors.getvalue()


# --- Одна цена на всех экранах -------------------------------------------------


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
        "cell": get_or_create_location("S07-D01-C01", name="Ячейка цен"),
    }


def _part(env, *, name="ПОРШЕНЬ ЦЕНА", article="01.1395.100", price="13100"):
    part = PartType.objects.create(
        name=name, category=env["category"], unit=Unit.objects.get(name="Штука"),
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal(price) if price is not None else None,
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


def _draft(client, env, part, kind, quantity="2"):
    cart = open_cart(kind, by=env["admin"])
    add_scan(cart, part, env["cell"], quantity=Decimal(quantity), by=env["admin"])
    session = client.session
    session[CART_SESSION_KEYS[kind]] = cart.pk
    session.save()
    return cart


@pytest.mark.parametrize("kind", ["sale", "repair"])
def test_the_draft_names_the_unit_price_and_the_line_total(client, env, kind):
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])
    cart = _draft(client, env, part, kind)

    body = client.get(reverse("actions_scan"), {"kind": kind}).content.decode()

    assert '<th class="num--money">Цена</th>' in body
    assert '<th class="num--money">Сумма</th>' in body
    assert "Цена клиенту" not in body  # третьего названия у одной сущности нет
    assert "Рекомендуемая цена" not in body and "Минимальная цена" not in body

    row = cart_rows(cart)[0]
    assert row.unit_price == part.recommended_price  # каноническая цена детали
    assert row.total_price == part.recommended_price * 2
    assert "13 100 ₽" in body.replace(" ", " ")  # цена за единицу
    assert "26 200 ₽" in body.replace(" ", " ")  # итог строки


def test_the_repair_draft_still_lets_the_operator_correct_the_price(client, env):
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])
    _draft(client, env, part, "repair")
    body = client.get(reverse("actions_scan"), {"kind": "repair"}).content.decode()
    assert 'name="unit_price"' in body
    assert 'aria-label="Цена"' in body


def test_a_part_without_a_price_shows_a_dash_not_a_zero(client, env):
    """У ремонта цена может отсутствовать: тогда это прочерк, а не ноль."""
    part = _part(env, name="БЕЗ ЦЕНЫ", article="BEZ-CENY-1", price=None)
    _stock(env, part)
    client.force_login(env["admin"])
    cart = _draft(client, env, part, "repair")

    body = client.get(reverse("actions_scan"), {"kind": "repair"}).content.decode()
    assert cart_rows(cart)[0].unit_price is None
    assert "—" in body
    assert "0 ₽" not in body


def test_a_sale_line_records_zero_when_the_part_has_no_price(client, env):
    """Разница продажи и ремонта, зафиксированная сознательно.

    У строки продажи цена обязательна на уровне модели, поэтому деталь без
    канонической цены попадает в черновик с нулём, и оператор видит «0 ₽», а
    не прочерк. У ремонта цена необязательна, там остаётся прочерк. Сделать
    продажу такой же нельзя без миграции и решения о том, продаётся ли вообще
    деталь без цены, поэтому поведение здесь закреплено, а не подправлено
    отображением.
    """
    part = _part(env, name="БЕЗ ЦЕНЫ ПРОДАЖА", article="BEZ-CENY-2", price=None)
    _stock(env, part)
    client.force_login(env["admin"])
    cart = _draft(client, env, part, "sale")
    assert cart_rows(cart)[0].unit_price == Decimal("0.00")


def test_the_same_price_appears_in_search_and_on_the_card(client, env):
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])

    search = client.get(reverse("part_search"), {"q": "01.1395.100"}).content.decode()
    card = client.get(reverse("part_detail", args=[part.pk])).content.decode()

    for body in (search, card):
        assert "13 100" in body.replace(" ", " ")
        assert "Цена" in body
        assert "Рекомендуемая цена" not in body
        assert "Минимальная цена" not in body


def test_the_customer_selector_survived_the_price_change(client, env):
    Customer.objects.create(name="Саликов Рим Васильевич")
    part = _part(env)
    _stock(env, part)
    client.force_login(env["admin"])
    _draft(client, env, part, "sale")
    body = client.get(reverse("actions_scan"), {"kind": "sale"}).content.decode()
    assert 'name="customer_id"' in body
    assert "Саликов Рим Васильевич" in body
    assert "Создать клиента" in body
