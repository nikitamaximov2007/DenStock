"""Focused V1 customer-account contracts.

The account is deliberately MAX-only for website authentication. Telegram is
available only as a linkable messaging identity after MAX authentication.
"""

import pytest
from django.test import Client, override_settings
from django.urls import reverse

from apps.customer_accounts import services
from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerIdentity,
    CustomerLoginAttempt,
    CustomerSession,
    Provider,
)

ACCOUNT_ON = {
    "CUSTOMER_ACCOUNT_ENABLED": True,
    "CUSTOMER_AUTH_MAX_ENABLED": True,
    "MAX_BOT_USERNAME": "prostor_bot",
    "CUSTOMER_ACCOUNT_CONSENT_VERSION": "",
}


@pytest.mark.django_db
def test_website_auth_is_max_only_and_telegram_is_link_only(settings):
    with override_settings(**ACCOUNT_ON):
        assert services.login_enabled(Provider.MAX)
        assert not services.login_enabled(Provider.TELEGRAM)
        assert services.link_enabled(Provider.TELEGRAM)
        assert not services.link_enabled(Provider.MAX)


@pytest.mark.django_db
def test_max_code_flow_creates_reusable_account_and_session(settings):
    with override_settings(**ACCOUNT_ON):
        attempt = services.create_attempt(
            purpose=CustomerLoginAttempt.Purpose.LOGIN,
            provider=Provider.MAX,
            client_key="test-client",
        )
        reply = services.provider_confirmed(
            provider=Provider.MAX,
            token=attempt.token,
            provider_user_id=7110001,
            chat_id=7110002,
            display_name="Тестовый клиент",
        )
        assert reply.code and len(reply.code) == 6

        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret,
            code=reply.code,
        )
        assert completion.ok
        account = CustomerAccount.objects.get()
        assert account.display_name == "Тестовый клиент"
        assert CustomerIdentity.objects.get(
            provider=Provider.MAX, provider_user_id=7110001
        ).account_id == account.pk
        assert CustomerSession.objects.filter(account=account).count() == 1

        replay = services.complete_attempt(
            browser_secret=attempt.browser_secret,
            code=reply.code,
        )
        assert not replay.ok
        assert replay.outcome == services.Outcome.INVALID


@pytest.mark.django_db
def test_telegram_link_requires_existing_max_account(settings):
    with override_settings(**ACCOUNT_ON):
        account = CustomerAccount.objects.create(display_name="Клиент")
        attempt = services.create_attempt(
            purpose=CustomerLoginAttempt.Purpose.LINK,
            provider=Provider.TELEGRAM,
            client_key="test-client",
            account=account,
        )
        reply = services.provider_confirmed(
            provider=Provider.TELEGRAM,
            token=attempt.token,
            provider_user_id=7110003,
            chat_id=7110004,
            display_name="Telegram клиент",
        )
        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret,
            code=reply.code,
            session_token="missing-session",
        )
        assert not completion.ok
        assert CustomerIdentity.objects.filter(provider=Provider.TELEGRAM).count() == 0


@pytest.mark.django_db
def test_disabled_account_keeps_existing_messenger_handoff_unchanged(settings):
    with override_settings(CUSTOMER_ACCOUNT_ENABLED=False, CUSTOMER_AUTH_MAX_ENABLED=False):
        assert services.identity_account(Provider.MAX, 7110005) is None
        assert CustomerAccount.objects.count() == 0


@pytest.mark.django_db
def test_account_pages_are_hidden_until_enabled(settings):
    client = Client()
    with override_settings(CUSTOMER_ACCOUNT_ENABLED=False, ROOT_URLCONF="config.public_urls"):
        response = client.get(reverse("customer_account_login"))
    assert response.status_code == 404
