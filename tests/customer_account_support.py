"""Shared builders for the PRO-STOR customer account V1 qualification.

The account runs on the PUBLIC runtime, so every page test goes through
``public_client`` (public URLconf, middleware, cookies and context processors)
rather than the internal stack. ``account_on`` turns the feature flags on for
one test; nothing here changes a default.
"""

from decimal import Decimal

import pytest
from django.test import override_settings
from django.utils import timezone

from apps.customer_accounts import services, tokens, web_session
from apps.customer_accounts.models import CustomerLoginAttempt, Provider
from apps.customer_requests.models import (
    CustomerRequest,
    MaxConversation,
    TelegramConversation,
)
from apps.customer_requests.services import RequestLineInput, create_customer_request
from tests.public_catalog_support import public_runtime_settings

ACCOUNT_ON = {
    "CUSTOMER_ACCOUNT_ENABLED": True,
    "CUSTOMER_AUTH_MAX_ENABLED": True,
    "MAX_BOT_USERNAME": "prostor_bot",
    "TELEGRAM_BOT_USERNAME": "prostor_tg_bot",
    "CUSTOMER_ACCOUNT_CONSENT_VERSION": "",
}
ACCOUNT_OFF = {
    "CUSTOMER_ACCOUNT_ENABLED": False,
    "CUSTOMER_AUTH_MAX_ENABLED": False,
}


def account_on(**extra):
    return override_settings(**{**ACCOUNT_ON, **extra})


def public_account_runtime(**extra):
    """The public runtime with the account feature on."""
    return public_runtime_settings(**{**ACCOUNT_ON, **extra})


@pytest.fixture
def account_client(public_client):
    """The public test client, with the account feature enabled."""
    with account_on():
        yield public_client


# --- Sign in -------------------------------------------------------------------------------


def sign_in(user_id, *, name="Клиент MAX", chat_id=None):
    """The whole MAX login: attempt → bot confirms → code typed. Returns the token."""
    attempt = services.create_attempt(
        purpose=CustomerLoginAttempt.Purpose.LOGIN,
        provider=Provider.MAX,
        client_key=f"client-{user_id}",
    )
    reply = services.provider_confirmed(
        provider=Provider.MAX,
        token=attempt.token,
        provider_user_id=user_id,
        chat_id=chat_id if chat_id is not None else user_id + 1,
        display_name=name,
    )
    completion = services.complete_attempt(
        browser_secret=attempt.browser_secret, code=reply.code
    )
    assert completion.ok, completion.outcome
    return completion.session_token


def as_account(client, session_token):
    """Put a live account session on a test client, the way the browser does."""
    client.cookies[web_session.ACCOUNT_COOKIE] = session_token
    return client


def start_login_attempt(*, purpose=CustomerLoginAttempt.Purpose.LOGIN, provider=Provider.MAX,
                        account=None, client_key="client-1"):
    return services.create_attempt(
        purpose=purpose, provider=provider, client_key=client_key, account=account
    )


def set_login_cookie(client, attempt):
    client.cookies[web_session.LOGIN_COOKIE] = f"{attempt.browser_secret}.{attempt.token}"
    return client


# --- Requests ------------------------------------------------------------------------------


def make_request(catalog, part, *, name="Клиент", phone="+7 912 000-00-01",
                 messenger=CustomerRequest.Messenger.MAX, quantity="2", key=None):
    request, _created = create_customer_request(
        customer_name=name,
        customer_phone=phone,
        preferred_messenger=messenger,
        lines=[RequestLineInput(part_id=part.pk, quantity=Decimal(quantity))],
        privacy_policy_version="pp-1",
        personal_data_consent_version="pd-1",
        # The submission key must be at least 16 characters (idempotency).
        submission_key=f"submission-key-{key or part.pk}-{name}-{quantity}",
    )
    return request


def link_max_conversation(request, user_id):
    """What the MAX handoff records once the messenger's server verified the user."""
    return MaxConversation.objects.create(
        request=request,
        status=MaxConversation.Status.LINKED,
        customer_user_id=user_id,
        customer_chat_id=user_id + 1,
        linked_at=timezone.now(),
    )


def link_telegram_conversation(request, user_id):
    return TelegramConversation.objects.create(
        request=request,
        status=TelegramConversation.Status.LINKED,
        customer_user_id=user_id,
        customer_chat_id=user_id + 1,
        linked_at=timezone.now(),
    )


# --- Sales ---------------------------------------------------------------------------------


def make_customer(name="Покупатель"):
    from apps.customers.models import Customer

    return Customer.objects.create(name=name)


# Distinctive internal money a customer must never see. Deliberately plain
# integers so a leak is findable in rendered HTML whatever the price filter does.
INTERNAL_UNIT_COST = Decimal("6161")
INTERNAL_COST = Decimal("7171")
INTERNAL_PROFIT = Decimal("8181")


def make_sale(customer, part, *, lot, quantity="2", unit_price="1000", status=None):
    """A completed DenisStock sale with one line, plus the internal totals a
    customer must never see (cost, profit, landed cost)."""
    from apps.sales.models import Sale, SaleLine

    quantity, unit_price = Decimal(quantity), Decimal(unit_price)
    total = quantity * unit_price
    sale = Sale.objects.create(
        customer=customer,
        customer_name=customer.name,
        status=status or Sale.Status.COMPLETED,
        sold_at=timezone.now(),
        revenue_total=total,
        cost_total=INTERNAL_COST,
        profit_total=INTERNAL_PROFIT,
    )
    SaleLine.objects.create(
        sale=sale,
        part_type=part,
        stock_lot=lot,
        batch=lot.batch_line.batch,
        batch_line=lot.batch_line,
        quantity=quantity,
        unit_price=unit_price,
        total_price=total,
        unit_cost_rub=INTERNAL_UNIT_COST,
        total_cost_rub=INTERNAL_COST,
        profit_rub=INTERNAL_PROFIT,
    )
    return sale


def link_customer_card(account, customer, user):
    return services.link_customer(account, customer, by_user=user)


__all__ = [
    "ACCOUNT_ON",
    "ACCOUNT_OFF",
    "account_client",
    "account_on",
    "as_account",
    "link_customer_card",
    "link_max_conversation",
    "link_telegram_conversation",
    "make_customer",
    "make_request",
    "make_sale",
    "INTERNAL_COST",
    "INTERNAL_PROFIT",
    "INTERNAL_UNIT_COST",
    "public_account_runtime",
    "set_login_cookie",
    "sign_in",
    "start_login_attempt",
    "tokens",
]
