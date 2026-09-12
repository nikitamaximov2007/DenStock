"""Единая запись российского телефона: канон на сервере и маска в браузере.

Что здесь гарантируется:

* канонический вид «+7 900 123-45-67» получается из любой привычной записи
  («89001234567», «79001234567», «+79001234567», «9001234567») и считается
  СЕРВЕРОМ, поэтому не зависит от JS;
* иностранный номер, короткий внутренний и номер с добавочным остаются ровно
  тем текстом, который ввели: чужой формат не угадывается;
* уже сохранённые номера канон не переписывает - он живёт в слое ввода;
* поле телефона везде одно и то же: телефонная клавиатура на мобильном,
  подсказка, связанная с полем, и общая маска;
* сама маска (static/js/phone_input.js) проверяется прогоном в node, а при его
  отсутствии - статически, как и остальной JS проекта.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings
from django.core.cache import cache
from django.urls import reverse

from apps.core.phones import canonical_phone_text, format_ru_phone, normalize_phone
from apps.customer_requests.models import CustomerRequest
from apps.customers.models import Customer
from apps.repairs.forms import RepairOrderForm
from apps.sales.forms import ReservationForm, SaleForm

PASSWORD = "parol-12345"
JS_PATH = Path(settings.BASE_DIR) / "static" / "js" / "phone_input.js"
JS = JS_PATH.read_text(encoding="utf-8")
CANONICAL = "+7 900 123-45-67"
SAME_NUMBER = ["89001234567", "79001234567", "+79001234567", "9001234567", CANONICAL]


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    """Публичная заявка ограничена по адресу: счётчик не должен течь между тестами."""
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, is_superuser=True):
        if is_superuser:
            return django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        return django_user_model.objects.create_user(username=username, password=PASSWORD)

    return _make


@pytest.fixture
def boss(make_user, client):
    make_user("boss")
    client.login(username="boss", password=PASSWORD)
    return client


# --- Канон на сервере ----------------------------------------------------------------------


@pytest.mark.parametrize("raw", SAME_NUMBER)
def test_every_usual_russian_form_becomes_one_record(raw):
    assert format_ru_phone(raw) == CANONICAL
    assert canonical_phone_text(raw) == CANONICAL


def test_canonical_record_keeps_the_digits_of_the_number():
    assert normalize_phone(canonical_phone_text("89001234567")) == "79001234567"


def test_landline_with_the_country_code_uses_the_same_record():
    assert canonical_phone_text("8 495 000 11 22") == "+7 495 000-11-22"


def test_canonical_is_idempotent():
    assert canonical_phone_text(CANONICAL) == CANONICAL
    assert canonical_phone_text(canonical_phone_text("9001234567")) == CANONICAL


@pytest.mark.parametrize(
    "raw",
    [
        "+49 30 123456",  # немецкий номер, а не московский без восьмёрки
        "4930123456",
        "+1 202 555 0134",
        "12345",
        "+7 900 123-45-67 доб. 123",  # цифр больше, чем в номере
        "",
    ],
)
def test_anything_not_provably_russian_stays_as_typed(raw):
    assert format_ru_phone(raw) == ""
    assert canonical_phone_text(raw) == raw.strip()


# --- Карточка клиента ----------------------------------------------------------------------


def test_customer_card_stores_the_canonical_record(boss, db):
    boss.post(
        reverse("customer_create"),
        {"name": "Иванов", "phone": "89001234567", "comment": ""},
        follow=True,
    )
    customer = Customer.objects.get(name="Иванов")
    assert customer.phone == CANONICAL
    assert customer.phone_normalized == "79001234567"


def test_customer_card_keeps_a_foreign_number_as_typed(boss, db):
    boss.post(
        reverse("customer_create"),
        {"name": "Шмидт", "phone": "+49 30 123456", "comment": ""},
        follow=True,
    )
    assert Customer.objects.get(name="Шмидт").phone == "+49 30 123456"


def test_an_old_record_is_rewritten_only_when_the_card_is_saved(boss, db):
    """Миграции нет: старая запись живёт до первой ручной правки карточки."""
    customer = Customer.objects.create(name="Петров", phone="89001234567")
    assert customer.phone == "89001234567"

    boss.post(
        reverse("customer_edit", args=[customer.pk]),
        {"name": "Петров", "phone": customer.phone, "comment": ""},
        follow=True,
    )
    customer.refresh_from_db()
    assert customer.phone == CANONICAL


# --- Документы: продажа, резерв, ремонт ----------------------------------------------------


@pytest.mark.parametrize("form_class", [ReservationForm, SaleForm, RepairOrderForm])
def test_document_forms_canonicalise_a_hand_typed_phone(form_class, db):
    form = form_class(
        data={
            "customer_name": "Иван Петров",
            "customer_phone": "89001234567",
            "problem_description": "Не заводится.",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["customer_phone"] == CANONICAL


@pytest.mark.parametrize("form_class", [ReservationForm, SaleForm, RepairOrderForm])
def test_a_chosen_card_keeps_its_own_record_in_the_document(form_class, db):
    """Снимок обязан совпадать с карточкой, включая старую запись номера."""
    customer = Customer.objects.create(name="Сидоров", phone="89001234567")
    form = form_class(
        data={
            "customer": str(customer.pk),
            "customer_name": "",
            "customer_phone": "",
            "problem_description": "Не заводится.",
        }
    )
    assert form.is_valid(), form.errors
    assert form.cleaned_data["customer_phone"] == "89001234567"


# --- Поле в интерфейсе ---------------------------------------------------------------------


def _phone_tag(html, marker):
    """Весь тег <input ...>, внутри которого встретился маркер."""
    position = html.index(marker)
    start = html.rindex("<input", 0, position)
    return html[start : html.index(">", position) + 1]


@pytest.mark.parametrize(
    "route", ["customer_create", "reservation_create", "sale_create", "repair_order_create"]
)
def test_every_phone_field_offers_the_phone_keyboard_and_the_linked_hint(boss, db, route):
    html = boss.get(reverse(route)).content.decode()
    tag = _phone_tag(html, 'data-phone-input="ru"')
    for attribute in ('type="tel"', 'inputmode="tel"', 'autocomplete="tel"'):
        assert attribute in tag, (route, attribute)
    described = tag.split('aria-describedby="', 1)[1].split('"', 1)[0]
    assert f'id="{described}"' in html, (route, described)
    assert "phone_input.js" in html, route


def test_the_public_request_form_carries_the_same_field(public_client, public_catalog):
    part = public_catalog.part("BELT", article="B-77", price="1000")
    public_catalog.stock(part, "1")
    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    html = public_client.get("/request/").content.decode()
    tag = _phone_tag(html, 'id="customer_phone"')
    for attribute in ('type="tel"', 'inputmode="tel"', 'data-phone-input="ru"'):
        assert attribute in tag, attribute
    described = tag.split('aria-describedby="', 1)[1].split('"', 1)[0]
    assert 'id="request-phone-hint"' in html and "request-phone-hint" in described.split()
    assert "js/phone_input.js" in html


def test_the_public_policy_allows_that_one_script_and_nothing_looser(public_client):
    csp = public_client.get("/")["Content-Security-Policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    assert "default-src 'none'" in csp
    assert "http" not in csp  # ни одного внешнего источника


# --- Публичная заявка ----------------------------------------------------------------------


TOKEN = 'name="submission_key" value="'


@pytest.mark.parametrize("raw", SAME_NUMBER)
def test_a_request_sent_without_js_gets_the_canonical_record(
    public_client, public_catalog, raw
):
    part = public_catalog.part("SEAL", article=f"S-{abs(hash(raw)) % 10000}", price="500")
    public_catalog.stock(part, "1")
    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    body = public_client.get("/request/").content.decode()
    token = body.split(TOKEN, 1)[1].split('"', 1)[0]

    response = public_client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Иван Петров",
            "customer_phone": raw,
            "preferred_messenger": "telegram",
            "comment": "",
            "consent": "1",
        },
    )

    assert response.status_code == 302, response.status_code
    created = CustomerRequest.objects.get()
    assert created.customer_phone == CANONICAL
    assert created.customer_phone_normalized == "79001234567"


def test_a_request_with_a_foreign_number_keeps_it_as_typed(public_client, public_catalog):
    part = public_catalog.part("HOSE", article="H-1", price="500")
    public_catalog.stock(part, "1")
    public_client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
    body = public_client.get("/request/").content.decode()
    token = body.split(TOKEN, 1)[1].split('"', 1)[0]

    response = public_client.post(
        "/request/submit/",
        {
            "submission_key": token,
            "customer_name": "Klaus Schmidt",
            "customer_phone": "+49 30 123456",
            "preferred_messenger": "telegram",
            "comment": "",
            "consent": "1",
        },
    )

    assert response.status_code == 302
    assert CustomerRequest.objects.get().customer_phone == "+49 30 123456"


# --- Сама маска ----------------------------------------------------------------------------


def test_the_mask_never_builds_a_second_country_code_by_hand():
    """Значение пересобирается из цифр, а не дописывается к прежнему тексту."""
    assert 'value +=' not in JS
    assert "replace(/\\D+/g" in JS
    assert "module.exports" in JS  # чистые функции доступны тесту


def test_the_mask_refuses_to_guess_a_foreign_number():
    assert "/^[789]$/" in JS
    assert "NATIONAL_LENGTH" in JS


@pytest.mark.parametrize(
    ("typed", "shown"),
    [
        ("89001234567", CANONICAL),
        ("79001234567", CANONICAL),
        ("+79001234567", CANONICAL),
        ("9001234567", CANONICAL),
        ("8 900 123 45 67", CANONICAL),
        ("9", "+7 9"),
        ("900", "+7 900"),
        ("8", "8"),
        ("", ""),
        ("+49 30 123456", "+49 30 123456"),
        ("4930123456", "4930123456"),
        ("+1 202 555 0134", "+1 202 555 0134"),
        ("900123456789", "900123456789"),
        ("8 495 000 11 22", "+7 495 000-11-22"),
    ],
)
def test_the_mask_shows_one_record_for_every_way_of_typing_it(typed, shown):
    assert _mask(typed) == shown


@pytest.mark.parametrize("keys", ["89001234567", "79001234567", "9001234567"])
def test_typing_digit_by_digit_ends_in_the_same_record(keys):
    value = ""
    for key in keys:
        value = _mask(value + key)
    assert value == CANONICAL


def test_the_field_can_always_be_cleared():
    value = CANONICAL
    for _ in range(40):
        if not value:
            break
        value = _mask(value[:-1])
    assert value == ""


def _mask(value):
    node = shutil.which("node")
    if node is None:  # pragma: no cover - зависит от машины
        pytest.skip("node не установлен: поведение маски проверяется отдельно")
    script = (
        "const m=require(process.argv[1]);"
        "process.stdout.write(JSON.stringify(m.maskValue(JSON.parse(process.argv[2]))));"
    )
    result = subprocess.run(
        [node, "-e", script, str(JS_PATH), json.dumps(value)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return json.loads(result.stdout)
