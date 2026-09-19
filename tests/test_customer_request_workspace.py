"""The operator workspace in DenisStock: the list, the card and one reply box.

Release A. Everything here is the production code; no network and no bot
worker. What a customer would see is produced by the same services the real
transports call, so the derived «Ждёт ответа» state is read from real history.
"""
import re
import uuid
from decimal import Decimal

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.db import connection
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.customer_requests import max_service, operator_replies, telegram_service, workspace
from apps.customer_requests.messengers import (
    consume_max_start,
    consume_telegram_start,
    issue_max_link,
    issue_telegram_link,
)
from apps.customer_requests.models import (
    CustomerRequest,
    MaxDeliveryStatus,
    MaxMessage,
    TelegramDeliveryStatus,
    TelegramMessage,
)
from apps.customer_requests.services import (
    RequestLineInput,
    change_request_status,
    create_customer_request,
)

PASSWORD = "parol-12345"
POLICY = "draft-legal-review-1"
CUSTOMER_CHAT = 5550001
MAX_USER = 6660001
MAX_CHAT = 6660002


@pytest.fixture
def part(db):
    category, _ = Category.objects.get_or_create(name="Двигатель", parent=None)
    result = PartType.objects.create(
        name="РЕМЕНЬ ПРИВОДНОЙ",
        category=category,
        unit=Unit.objects.get(name="Штука"),
        manufacturer=Manufacturer.objects.get_or_create(name="BRP")[0],
        tracking_mode=PartType.TrackingMode.BULK,
        recommended_price=Decimal("10000.00"),
        certified_price_rub=Decimal("10000.00"),
        price_provenance=PartType.PriceProvenance.FORMULA_CERTIFIED,
    )
    PartNumber.objects.create(part=result, value="448", is_primary=True)
    return result


@pytest.fixture
def seller(db, django_user_model):
    user = django_user_model.objects.create_user(username="denis", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.SELLER))
    return user


@pytest.fixture
def staff_client(client, seller):
    client.force_login(seller)
    return client


_keys = iter(f"{number:032x}" for number in range(1, 5000))


def make_request(
    part,
    *,
    messenger=CustomerRequest.Messenger.TELEGRAM,
    name="Иван Петров",
    phone="+7 (912) 123-45-67",
    price=True,
    quantity="2",
):
    request, _created = create_customer_request(
        customer_name=name,
        customer_phone=phone,
        preferred_messenger=messenger,
        comment="Нужна деталь.",
        # A supply inquiry keeps the request off stock; its price snapshot is
        # still the one the customer saw.
        lines=[RequestLineInput(part_id=part.pk, quantity=quantity, supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=next(_keys),
    )
    if not price:
        request.lines.update(price_seen=None)
    return request


def link(request, *, chat_id=CUSTOMER_CHAT, user_id=MAX_USER, max_chat=MAX_CHAT):
    """Bind the customer exactly as the real one-time link does."""
    if request.preferred_messenger == CustomerRequest.Messenger.MAX:
        token = issue_max_link(request_id=request.pk).token
        return consume_max_start(token=token, chat_id=max_chat, user_id=user_id)
    token = issue_telegram_link(request_id=request.pk).token
    return consume_telegram_start(token=token, chat_id=chat_id, user_id=chat_id, username="ivan")


_updates = iter(range(900_001, 999_999))
_mids = iter(f"mid.workspace{number}" for number in range(1, 5000))


def customer_writes(request, text):
    if request.preferred_messenger == CustomerRequest.Messenger.MAX:
        return max_service.record_customer_message(
            user_id=MAX_USER, chat_id=MAX_CHAT, mid=next(_mids), text=text
        )
    return telegram_service.record_customer_message(
        chat_id=CUSTOMER_CHAT, update_id=next(_updates), text=text
    )


def operator_answers(request, user, text, *, status=None):
    """Reply from DenisStock, then force the delivery state the test needs."""
    result = operator_replies.submit_reply(
        request_id=request.pk, user=user, text=text, key=uuid.uuid4().hex
    )
    if status is not None:
        type(result.message).objects.filter(pk=result.message.pk).update(delivery_status=status)
    return result


def reply_states(channel):
    if channel == CustomerRequest.Messenger.MAX:
        return MaxMessage, MaxDeliveryStatus
    return TelegramMessage, TelegramDeliveryStatus


def fetch(request):
    return workspace.annotated_request(request.pk)


def list_html(staff_client, **params):
    url = reverse("customer_request_list")
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return staff_client.get(f"{url}?{query}" if query else url).content.decode()


def detail_html(staff_client, request):
    return staff_client.get(
        reverse("customer_request_detail", args=[request.pk])
    ).content.decode()


def rows(html):
    return re.findall(r'data-request-row="([0-9A-F]+)"', html)


# --- The derived state ------------------------------------------------------------------


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_a_customer_message_makes_the_request_wait_and_a_delivered_reply_clears_it(
    part, seller, messenger
):
    request = make_request(part, messenger=messenger)
    link(request)
    assert fetch(request).needs_reply is False  # nothing asked yet

    customer_writes(request, "Когда можно забрать?")
    waiting = fetch(request)
    assert waiting.needs_reply is True
    assert waiting.attention == workspace.ATTENTION_WAITING
    assert waiting.customer_preview == "Когда можно забрать?"

    model, states = reply_states(messenger)
    operator_answers(request, seller, "Завтра с 10:00.", status=states.SENT)
    answered = fetch(request)
    assert answered.needs_reply is False
    assert answered.attention == workspace.ATTENTION_NONE

    customer_writes(request, "А в субботу?")
    assert fetch(request).needs_reply is True
    assert model.objects.filter(direction="operator_to_customer").count() == 1


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
@pytest.mark.parametrize("bad", ["failed", "uncertain"])
def test_a_reply_the_customer_may_never_have_seen_keeps_the_request_waiting(
    part, seller, messenger, bad
):
    request = make_request(part, messenger=messenger)
    link(request)
    customer_writes(request, "Есть в наличии?")
    operator_answers(request, seller, "Есть.", status=bad)

    row = fetch(request)
    assert row.needs_reply is True
    assert row.attention == workspace.ATTENTION_FAILED


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_a_reply_still_on_its_way_is_shown_as_sending_and_still_waits(part, seller, messenger):
    request = make_request(part, messenger=messenger)
    link(request)
    customer_writes(request, "Есть в наличии?")
    operator_answers(request, seller, "Сейчас уточню.")  # queued: pending

    row = fetch(request)
    assert row.needs_reply is True
    assert row.attention == workspace.ATTENTION_SENDING


def test_a_second_attempt_that_reaches_the_customer_clears_the_state(part, seller):
    """A failed reply, then one that was delivered: the customer has an answer."""
    request = make_request(part)
    link(request)
    customer_writes(request, "вопрос")
    operator_answers(request, seller, "первая попытка", status=TelegramDeliveryStatus.FAILED)
    assert fetch(request).attention == workspace.ATTENTION_FAILED

    operator_answers(request, seller, "вторая попытка", status=TelegramDeliveryStatus.SENT)

    row = fetch(request)
    assert row.needs_reply is False
    assert row.attention == workspace.ATTENTION_NONE


def test_a_question_after_a_failed_reply_still_waits(part, seller):
    request = make_request(part)
    link(request)
    customer_writes(request, "первый вопрос")
    operator_answers(request, seller, "не дошло", status=TelegramDeliveryStatus.FAILED)
    customer_writes(request, "вы тут?")

    row = fetch(request)
    assert row.needs_reply is True
    # The undelivered reply is older than the new question: plain waiting.
    assert row.attention == workspace.ATTENTION_WAITING
    assert row.customer_preview == "вы тут?"


def test_a_bot_message_is_not_an_employee_reply(part, seller):
    request = make_request(part)
    link(request)  # the linking summary is a system message
    customer_writes(request, "Здравствуйте")
    telegram_service.customer_greeting(CUSTOMER_CHAT)

    row = fetch(request)
    assert row.needs_reply is True
    assert TelegramMessage.objects.filter(direction="system").exists()


def test_a_closed_request_never_waits_for_anybody(part, seller, admin_user):
    request = make_request(part)
    link(request)
    customer_writes(request, "Уже не нужно")
    assert fetch(request).needs_reply is True

    change_request_status(request_id=request.pk, target_status="canceled", by=admin_user)

    row = fetch(request)
    assert row.needs_reply is False
    assert row.priority == workspace.PRIORITY_CLOSED
    # The history stays exactly as it was.
    assert TelegramMessage.objects.filter(direction="customer_to_operator").count() == 1


# --- List page --------------------------------------------------------------------------


def test_the_list_puts_the_longest_waiting_first_then_new_then_in_work_then_closed(
    part, seller, staff_client, admin_user
):
    waiting_old = make_request(part, name="Ждёт давно")
    waiting_new = make_request(part, name="Ждёт недавно")
    fresh = make_request(part, name="Новая")
    in_work = make_request(part, name="В работе")
    closed = make_request(part, name="Закрыта")
    for request in (waiting_old, waiting_new):
        link(request, chat_id=CUSTOMER_CHAT + request.pk)
    customer_writes_from(waiting_old, "первый вопрос", chat=CUSTOMER_CHAT + waiting_old.pk)
    customer_writes_from(waiting_new, "второй вопрос", chat=CUSTOMER_CHAT + waiting_new.pk)
    change_request_status(request_id=in_work.pk, target_status="in_progress", by=admin_user)
    change_request_status(request_id=closed.pk, target_status="canceled", by=admin_user)

    order = rows(list_html(staff_client, tab="all"))

    assert order == [
        waiting_old.reference,
        waiting_new.reference,
        fresh.reference,
        in_work.reference,
        closed.reference,
    ]


def customer_writes_from(request, text, *, chat):
    return telegram_service.record_customer_message(
        chat_id=chat, update_id=next(_updates), text=text
    )


def test_tabs_and_messenger_filters_select_and_count_the_right_requests(
    part, seller, staff_client, admin_user
):
    telegram_waiting = make_request(part, name="ТГ ждёт")
    link(telegram_waiting)
    customer_writes(telegram_waiting, "вопрос")
    max_new = make_request(part, messenger=CustomerRequest.Messenger.MAX, name="MAX новая")
    done = make_request(part, name="Выполнена")
    change_request_status(request_id=done.pk, target_status="in_progress", by=admin_user)
    change_request_status(request_id=done.pk, target_status="completed", by=admin_user)

    assert rows(list_html(staff_client)) == [telegram_waiting.reference, max_new.reference]
    assert rows(list_html(staff_client, tab="waiting")) == [telegram_waiting.reference]
    assert rows(list_html(staff_client, tab="new")) == [
        telegram_waiting.reference,
        max_new.reference,
    ]
    assert rows(list_html(staff_client, tab="completed")) == [done.reference]
    assert rows(list_html(staff_client, tab="canceled")) == []
    assert rows(list_html(staff_client, messenger="max")) == [max_new.reference]
    assert rows(list_html(staff_client, messenger="telegram")) == [telegram_waiting.reference]

    counts = workspace.tab_counts(messenger="", query="")
    assert counts == {
        "active": 2, "waiting": 1, "new": 2, "completed": 1, "canceled": 0, "all": 3
    }


def test_search_finds_a_request_by_number_name_phone_and_article(part, seller, staff_client):
    mine = make_request(part, name="Пётр Сидоров", phone="+7 (999) 111-22-33")
    other = make_request(part, name="Анна Крылова", phone="+7 (916) 444-55-66")

    assert rows(list_html(staff_client, tab="all", q=mine.reference)) == [mine.reference]
    assert rows(list_html(staff_client, tab="all", q="сидоров")) == [mine.reference]
    assert rows(list_html(staff_client, tab="all", q="9991112233")) == [mine.reference]
    assert set(rows(list_html(staff_client, tab="all", q="448"))) == {
        mine.reference,
        other.reference,
    }
    assert rows(list_html(staff_client, tab="all", q="нет-такого")) == []


def test_an_unknown_price_is_never_shown_as_zero(part, seller, staff_client):
    unknown = make_request(part, price=False)
    known = make_request(part, price=True)

    html = list_html(staff_client, tab="all")
    totals = re.findall(r"data-total>\s*([^<]+?)\s*</dd>", html)
    assert "Уточняется" in totals[0] or "Уточняется" in totals[1]
    assert not re.search(r"(?<![\d\s])0 ₽", html) and ">0 ₽" not in html

    detail = detail_html(staff_client, unknown)
    price_cells = re.findall(r"data-price-seen>\s*([^<]+?)\s*<", detail)
    assert price_cells == ["Уточняется"]
    assert re.search(r"data-request-total>\s*Уточняется", detail)
    assert "20 000 ₽" in detail_html(staff_client, known)


def test_the_list_badges_the_messenger_and_the_waiting_state(part, seller, staff_client):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    customer_writes(request, "жду ответа")

    html = list_html(staff_client)

    assert 'data-messenger-badge="max"' in html
    assert 'data-attention-badge>Ждёт ответа' in html
    assert "жду ответа" in html  # the preview of what the customer asked


def test_the_list_does_not_grow_a_query_per_request(
    part, seller, staff_client, django_assert_max_num_queries
):
    for _ in range(6):
        request = make_request(part)
        link(request, chat_id=CUSTOMER_CHAT + request.pk)
        customer_writes_from(request, "вопрос", chat=CUSTOMER_CHAT + request.pk)

    with django_assert_max_num_queries(14):
        staff_client.get(reverse("customer_request_list"))


# --- Detail page ------------------------------------------------------------------------


def test_the_card_shows_one_chronological_conversation_with_roles(part, seller, staff_client):
    request = make_request(part)
    link(request)
    customer_writes(request, "Здравствуйте, когда забрать?")
    operator_answers(request, seller, "Сегодня после 15:00.", status=TelegramDeliveryStatus.SENT)

    html = detail_html(staff_client, request)

    customer = html.index("Здравствуйте, когда забрать?")
    reply = html.index("Сегодня после 15:00.")
    assert html.index("Ваша заявка №") < customer < reply
    assert 'data-role="customer"' in html and 'data-role="operator"' in html
    assert "Отправлено" in html
    assert "denis" in html  # the employee is named to employees
    for debug in ("callback_id", "dedupe", "mid.", "update_id"):
        assert debug not in html


def test_the_card_shows_immutable_line_prices_and_the_request_total(part, seller, staff_client):
    request = make_request(part)
    html = detail_html(staff_client, request)

    assert "448" in html and "РЕМЕНЬ ПРИВОДНОЙ" in html
    assert "10 000 ₽" in html  # price_seen
    assert "20 000 ₽" in html  # line total and request total
    assert "data-request-total" in html


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_an_employee_answers_from_the_card_whatever_the_messenger(
    part, seller, staff_client, messenger
):
    request = make_request(part, messenger=messenger)
    link(request)
    customer_writes(request, "Есть в наличии?")
    page = detail_html(staff_client, request)
    assert "data-reply-form" in page
    key = re.search(r'name="submission_key" value="([0-9a-f]{32})"', page).group(1)

    response = staff_client.post(
        reverse("customer_request_reply", args=[request.pk]),
        {"text": "Да, есть. Ждём вас.", "submission_key": key},
    )

    assert response.status_code == 302
    model, states = reply_states(messenger)
    reply = model.objects.get(direction="operator_to_customer")
    assert reply.text == "Да, есть. Ждём вас."
    assert reply.operator_user == seller
    assert reply.delivery_status == states.PENDING  # the worker delivers it
    assert reply.conversation.request_id == request.pk


@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_a_double_submit_sends_the_customer_one_reply(part, seller, staff_client, messenger):
    request = make_request(part, messenger=messenger)
    link(request)
    key = uuid.uuid4().hex

    for _ in range(2):
        response = staff_client.post(
            reverse("customer_request_reply", args=[request.pk]),
            {"text": "Один ответ", "submission_key": key},
        )
        assert response.status_code == 302

    model, _states = reply_states(messenger)
    assert model.objects.filter(direction="operator_to_customer").count() == 1


def test_the_old_max_reply_url_still_answers_the_same_customer(part, seller, staff_client):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)

    response = staff_client.post(
        reverse("customer_request_max_reply", args=[request.pk]),
        {"text": "Ответ по старой ссылке", "submission_key": uuid.uuid4().hex},
    )

    assert response.status_code == 302
    assert MaxMessage.objects.filter(text="Ответ по старой ссылке").count() == 1


def test_a_closed_request_offers_no_reply_box_and_refuses_a_posted_one(
    part, seller, staff_client, admin_user
):
    request = make_request(part)
    link(request)
    customer_writes(request, "вопрос")
    change_request_status(request_id=request.pk, target_status="canceled", by=admin_user)

    page = detail_html(staff_client, request)
    assert "data-reply-form" not in page
    assert f"Заявка №{request.reference} уже закрыта." in page

    response = staff_client.post(
        reverse("customer_request_reply", args=[request.pk]),
        {"text": "поздно", "submission_key": uuid.uuid4().hex},
    )

    assert response.status_code == 200  # the page comes back with the reason
    assert not TelegramMessage.objects.filter(direction="operator_to_customer").exists()


def test_a_refused_reply_keeps_what_the_employee_typed(part, seller, staff_client):
    request = make_request(part)  # nobody linked: there is nowhere to send
    response = staff_client.post(
        reverse("customer_request_reply", args=[request.pk]),
        {"text": "Длинный ответ, который не хочется набирать заново",
         "submission_key": uuid.uuid4().hex},
    )

    page = response.content.decode()
    assert "data-unsent-text" in page
    assert "Длинный ответ, который не хочется набирать заново" in page
    assert "Клиент ещё не подключил Telegram" in page


def test_only_an_employee_with_sales_rights_can_open_or_answer(
    part, db, client, django_user_model
):
    request = make_request(part)
    link(request)
    viewer = django_user_model.objects.create_user(username="viewer", password=PASSWORD)
    viewer.groups.add(Group.objects.get(name=roles.VIEWER))
    client.force_login(viewer)

    assert client.get(reverse("customer_request_list")).status_code == 403
    assert client.get(reverse("customer_request_detail", args=[request.pk])).status_code == 403
    response = client.post(
        reverse("customer_request_reply", args=[request.pk]),
        {"text": "нельзя", "submission_key": uuid.uuid4().hex},
    )
    assert response.status_code == 403
    assert not TelegramMessage.objects.filter(direction="operator_to_customer").exists()


def test_filters_survive_opening_a_request_and_answering_from_it(part, seller, staff_client):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    customer_writes(request, "вопрос")

    html = list_html(staff_client, tab="waiting", messenger="max")
    detail_link = re.search(r'href="([^"]*customer-requests/\d+/[^"]*)"', html).group(1)
    detail_link = detail_link.replace("&amp;", "&")
    assert "tab=waiting" in detail_link and "messenger=max" in detail_link

    page = staff_client.get(detail_link).content.decode()
    assert "tab=waiting" in page and "messenger=max" in page
    response = staff_client.post(
        reverse("customer_request_reply", args=[request.pk]) + "?tab=waiting&messenger=max",
        {"text": "Ответ", "submission_key": uuid.uuid4().hex},
    )
    assert "tab=waiting" in response["Location"] and "messenger=max" in response["Location"]


def test_the_customer_never_sees_the_employee_behind_the_reply(part, seller, staff_client):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    operator_answers(request, seller, "Ответ клиенту")

    outgoing = MaxMessage.objects.get(direction="operator_to_customer")
    assert outgoing.text == "Ответ клиенту"
    assert "denis" not in outgoing.text
    assert outgoing.operator_user == seller  # audit keeps who wrote it


def test_an_employee_reply_is_announced_to_colleagues_only(part, seller, staff_client, db):
    request = make_request(part, messenger=CustomerRequest.Messenger.MAX)
    link(request)
    result = operator_answers(request, seller, "Ответ")

    event = result.message.events.get()
    assert event.kind == "operator_reply"
    assert event.exclude_user == seller


def test_status_actions_keep_the_stage_0_rule_for_the_customer(part, seller, staff_client):
    request = make_request(part)
    link(request)
    customer_writes(request, "первый вопрос")

    staff_client.post(
        reverse("customer_request_status", args=[request.pk]), {"status": "canceled"}
    )

    request.refresh_from_db()
    assert request.status == CustomerRequest.Status.CANCELED
    result = telegram_service.record_customer_message(
        chat_id=CUSTOMER_CHAT, update_id=next(_updates), text="ещё вопрос"
    )
    assert f"Заявка №{request.reference} уже закрыта." in result.reply
    assert TelegramMessage.objects.filter(text="ещё вопрос").count() == 0
    assert TelegramMessage.objects.filter(text="первый вопрос").count() == 1


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="PostgreSQL concurrency integration test"
)
@pytest.mark.parametrize(
    "messenger", [CustomerRequest.Messenger.TELEGRAM, CustomerRequest.Messenger.MAX]
)
def test_two_clicks_at_the_same_moment_still_send_one_reply(part, seller, messenger):
    """The real double click: two requests in flight at once, one customer message."""
    import threading

    from django.db import connections

    request = make_request(part, messenger=messenger)
    link(request)
    key = uuid.uuid4().hex
    barrier = threading.Barrier(2)
    outcomes = []

    def submit():
        barrier.wait()
        try:
            result = operator_replies.submit_reply(
                request_id=request.pk, user=seller, text="Один ответ", key=key
            )
            outcomes.append(result.created)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    model, _states = reply_states(messenger)
    assert model.objects.filter(direction="operator_to_customer").count() == 1
    assert sorted(outcomes) == [False, True]  # one stored it, one found it


def test_realtime_composer_enter_contract_is_delegated_and_ime_safe():
    source = (settings.BASE_DIR / "static" / "js" / "customer_requests_realtime.js").read_text(
        encoding="utf-8"
    )
    assert "event.key === 'Enter' || event.code === 'Enter' || event.keyCode === 13" in source
    assert "event.shiftKey || composing || event.isComposing || event.keyCode === 229" in source
    assert "document.addEventListener('keydown', submitOnEnter, true)" in source
    assert "replyForm.requestSubmit()" in source
