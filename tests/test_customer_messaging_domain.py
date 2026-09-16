"""The customer messaging rules, exercised without any messenger.

These tests never parse a Telegram update and never touch a transport model.
What they prove is that the rules a customer notices — what the summary says,
when the bot acknowledges, which request a message belongs to — hold on their
own, so a second transport inherits behaviour rather than a copy of it.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from apps.catalog.models import Category, Manufacturer, PartNumber, PartType, Unit
from apps.customer_requests import messaging
from apps.customer_requests.models import CustomerRequest, CustomerRequestLine
from apps.customer_requests.services import RequestLineInput, create_customer_request

from .test_customer_requests import POLICY

# A transport that is deliberately not Telegram: same rules, different limits.
FAKE_POLICY = messaging.SummaryPolicy(
    linked_text="Готово. FAKE подключён к заявке {reference}.", message_limit=4000
)


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


def _request(part, *, key):
    request, created = create_customer_request(
        customer_name="Иван Петров",
        customer_phone="+7 (912) 123-45-67",
        preferred_messenger=CustomerRequest.Messenger.TELEGRAM,
        comment="",
        lines=[RequestLineInput(part_id=part.pk, quantity="2", supply_inquiry=True)],
        privacy_policy_version=POLICY,
        personal_data_consent_version=POLICY,
        submission_key=key,
    )
    assert created
    return request


def _line(article="A1", name="ДЕТАЛЬ", quantity="1", price="88", unit="шт"):
    return SimpleNamespace(
        article=article,
        part_name=name,
        quantity_requested=Decimal(quantity),
        unit_short_name=unit,
        price_seen=None if price is None else Decimal(price),
    )


def _fake_request(*lines, reference="ABCD1234"):
    return SimpleNamespace(
        reference=reference, lines=SimpleNamespace(order_by=lambda *_: list(lines))
    )


# --- Request summary ---------------------------------------------------------------------


def test_a_single_line_request_reads_as_one_priced_order():
    messages = messaging.request_summary_messages(_fake_request(_line()), FAKE_POLICY)

    assert len(messages) == 1
    assert "Готово. FAKE подключён к заявке ABCD1234." in messages[0]
    assert "A1 — ДЕТАЛЬ\n1 шт. × 88 ₽ = 88 ₽" in messages[0]
    assert "Итого: 88 ₽" in messages[0]
    assert messages[0].endswith("Можете написать вопрос прямо сейчас.")


def test_every_line_of_a_multi_line_request_is_present_with_its_own_total():
    request = _fake_request(
        _line(article="390402300", name="RIVET_POP", quantity="1", price="88"),
        _line(article="421000667", name="ВТОРАЯ", quantity="2", price="45000"),
    )

    summary = "\n".join(messaging.request_summary_messages(request, FAKE_POLICY))

    assert "390402300 — RIVET_POP\n1 шт. × 88 ₽ = 88 ₽" in summary
    assert "421000667 — ВТОРАЯ\n2 шт. × 45 000 ₽ = 90 000 ₽" in summary
    assert "Итого: 90 088 ₽" in summary


def test_the_summary_quotes_the_price_the_customer_was_shown(part, db):
    request = _request(part, key="p" * 32)
    part.recommended_price = Decimal("999999.00")
    part.save(update_fields=["recommended_price"])

    summary = "\n".join(messaging.request_summary_messages(request, FAKE_POLICY))

    assert "448 — РЕМЕНЬ ПРИВОДНОЙ" in summary
    assert "2 шт. × 10 000 ₽ = 20 000 ₽" in summary
    assert "999 999" not in summary


def test_an_unknown_price_is_said_plainly_and_never_totalled_as_zero():
    request = _fake_request(_line(price=None), _line(article="A2", price=None))

    summary = "\n".join(messaging.request_summary_messages(request, FAKE_POLICY))

    assert "1 шт. — цена уточняется" in summary
    assert "Итого:" not in summary
    assert "0 ₽" not in summary


def test_a_mixed_request_totals_only_what_is_known_and_says_so():
    request = _fake_request(_line(price=None), _line(article="A2", quantity="2", price="45000"))

    summary = "\n".join(messaging.request_summary_messages(request, FAKE_POLICY))

    assert "Итого по позициям с известной ценой: 90 000 ₽" in summary
    assert "Есть позиции, цена которых уточняется." in summary
    assert "Итого: 0 ₽" not in summary


def test_a_long_order_is_split_between_whole_lines_within_the_transports_limit():
    lines = [_line(article=f"A{i:02d}", name="ДЕТАЛЬ " + "X" * 90) for i in range(12)]
    narrow = messaging.SummaryPolicy(linked_text=FAKE_POLICY.linked_text, message_limit=420)

    messages = messaging.request_summary_messages(_fake_request(*lines), narrow)

    assert len(messages) > 1
    assert all(len(message) <= 420 for message in messages)
    assert all(sum(f"A{i:02d}" in message for message in messages) == 1 for i in range(12))
    assert messages[-1].endswith("Можете написать вопрос прямо сейчас.")


def test_the_same_request_is_one_message_for_a_roomier_transport():
    lines = [_line(article=f"A{i:02d}", name="ДЕТАЛЬ " + "X" * 90) for i in range(12)]

    roomy = messaging.request_summary_messages(_fake_request(*lines), FAKE_POLICY)

    assert len(roomy) == 1


# --- First-message acknowledgement -------------------------------------------------------


def test_the_first_message_of_a_request_is_acknowledged_once():
    assert messaging.acknowledgement_for(is_first_customer_message=True) == (
        messaging.CUSTOMER_ACK_TEXT
    )


def test_later_messages_are_carried_without_repeating_the_acknowledgement():
    assert messaging.acknowledgement_for(is_first_customer_message=False) == ""


# --- Routing a plain message -------------------------------------------------------------


def test_a_customer_with_one_request_needs_no_choice():
    only = SimpleNamespace(pk=1)

    routing = messaging.route_customer_message([only], active_id=None)

    assert routing.conversation is only
    assert routing.ambiguous is False
    assert routing.resolved is True


def test_a_chosen_request_keeps_receiving_the_customers_messages():
    first, second = SimpleNamespace(pk=1), SimpleNamespace(pk=2)

    routing = messaging.route_customer_message([first, second], active_id=2)

    assert routing.conversation is second
    assert routing.ambiguous is False


def test_several_requests_and_no_choice_is_a_question_not_a_guess():
    routing = messaging.route_customer_message(
        [SimpleNamespace(pk=1), SimpleNamespace(pk=2)], active_id=None
    )

    assert routing.conversation is None
    assert routing.ambiguous is True
    assert routing.resolved is False


def test_a_stale_choice_does_not_silently_pick_someone_elses_request():
    first, second = SimpleNamespace(pk=1), SimpleNamespace(pk=2)

    routing = messaging.route_customer_message([first, second], active_id=999)

    assert routing.conversation is None
    assert routing.ambiguous is True


def test_a_customer_with_no_linked_request_is_not_ambiguous_merely_unknown():
    routing = messaging.route_customer_message([], active_id=None)

    assert routing.conversation is None
    assert routing.ambiguous is False


# --- Contact permission ------------------------------------------------------------------


def test_contact_stops_when_consent_is_withdrawn_or_the_request_is_anonymized(part, db):
    request = _request(part, key="c" * 32)
    assert messaging.customer_contact_allowed(request) is True

    request.consent_withdrawn_at = request.created_at
    assert messaging.customer_contact_allowed(request) is False

    request.consent_withdrawn_at = None
    request.data_anonymized_at = request.created_at
    assert messaging.customer_contact_allowed(request) is False


# --- External identity -------------------------------------------------------------------


def test_an_inbound_message_is_identified_within_its_own_transport():
    telegram = messaging.external_message_key("telegram", 902920071)
    max_message = messaging.external_message_key("max", "mid.abc123")

    assert telegram == "telegram:902920071"
    assert max_message == "max:mid.abc123"
    assert telegram != messaging.external_message_key("max", 902920071)


def test_a_transport_that_supplies_no_identity_is_refused():
    with pytest.raises(ValueError):
        messaging.external_message_key("max", "")
    with pytest.raises(ValueError):
        messaging.external_message_key("max", None)


# --- Cancellation keeps the request readable ---------------------------------------------


def test_a_cancelled_request_still_renders_its_order_for_history(part, db):
    request = _request(part, key="x" * 32)
    request.status = CustomerRequest.Status.CANCELED
    request.save(update_fields=["status"])

    summary = "\n".join(messaging.request_summary_messages(request, FAKE_POLICY))

    assert "448 — РЕМЕНЬ ПРИВОДНОЙ" in summary
    assert CustomerRequestLine.objects.filter(request=request).count() == 1
