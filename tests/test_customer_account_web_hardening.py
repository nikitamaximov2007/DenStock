"""The account's HTTP surface: cookies, CSRF, redirects, rate limit, consent.

The account runs on the PUBLIC runtime, beside an anonymous cart, so the tests
here go through that runtime's own middleware rather than the internal stack.
"""

import pytest
from django.test import Client
from django.urls import reverse

from apps.customer_accounts import services, tokens, web_session
from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerConsent,
    CustomerLoginAttempt,
    Provider,
)
from tests.customer_account_support import (
    as_account,
    public_account_runtime,
    sign_in,
    start_login_attempt,
)
from tests.public_catalog_support import PUBLIC_HOST

MAX_USER = 8700001


def _csrf_client():
    return Client(HTTP_HOST=PUBLIC_HOST, enforce_csrf_checks=True)


# --- Cookies ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_account_cookie_is_httponly_lax_and_not_the_employee_session(public_catalog):
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        attempt = start_login_attempt()
        reply = services.provider_confirmed(
            provider=Provider.MAX, token=attempt.token,
            provider_user_id=MAX_USER, chat_id=1, display_name="Клиент",
        )
        client.cookies[web_session.LOGIN_COOKIE] = (
            f"{attempt.browser_secret}.{attempt.token}"
        )
        response = client.post(
            reverse("customer_account_login_code"), {"code": reply.code}
        )
        assert response.status_code == 302
        cookie = response.cookies[web_session.ACCOUNT_COOKIE]
        assert cookie["httponly"] and cookie["samesite"] == "Lax"
        assert cookie["path"] == "/"
        assert "sessionid" not in response.cookies


@pytest.mark.django_db
def test_logout_clears_the_cookie_and_kills_the_session(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER)
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), token)
        response = client.post(reverse("customer_account_logout"))
        assert response.status_code == 302
        assert response.cookies[web_session.ACCOUNT_COOKIE].value == ""
        assert services.session_account(token) is None


@pytest.mark.django_db
def test_the_login_cookie_is_scoped_to_the_account_path(public_catalog):
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        response = client.post(reverse("customer_account_login_max"))
        cookie = response.cookies[web_session.LOGIN_COOKIE]
        assert cookie["path"] == "/account/" and cookie["httponly"]


@pytest.mark.django_db
def test_a_malformed_login_cookie_is_ignored(public_catalog):
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for bad in ["", ".", "nodot", "a" * 300 + ".b", "." + "b" * 300]:
            client.cookies[web_session.LOGIN_COOKIE] = bad
            response = client.get(reverse("customer_account_login_code"))
            assert response.status_code == 302
            assert response["Location"] == reverse("customer_account_login")


@pytest.mark.django_db
def test_an_oversized_account_cookie_is_ignored(public_catalog):
    with public_account_runtime():
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), "a" * 500)
        assert client.get(reverse("customer_account_home")).status_code == 302


# --- CSRF -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_state_changing_account_post_needs_a_csrf_token(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER)
        client = as_account(_csrf_client(), token)
        for name in ["customer_account_login_max", "customer_account_logout",
                     "customer_account_telegram_link", "customer_account_telegram_unlink",
                     "customer_account_profile", "customer_account_login_code"]:
            assert client.post(reverse(name)).status_code == 403, name


@pytest.mark.django_db
def test_a_reorder_post_needs_a_csrf_token(public_catalog):
    from tests.customer_account_support import link_customer_card, make_customer, make_sale

    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    lot = public_catalog.stock(part, "5")
    with public_account_runtime():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        customer = make_customer("Карточка")
        link_customer_card(account, customer, public_catalog.user)
        sale = make_sale(customer, part, lot=lot)
        client = as_account(_csrf_client(), token)
        assert client.post(
            reverse("customer_account_reorder", args=[sale.number])
        ).status_code == 403


@pytest.mark.django_db
def test_read_only_account_pages_refuse_unsafe_methods(public_catalog):
    with public_account_runtime():
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), sign_in(MAX_USER))
        for name in ["customer_account_home", "customer_account_requests",
                     "customer_account_purchases", "customer_account_messengers",
                     "customer_account_login"]:
            assert client.post(reverse(name)).status_code == 405, name


@pytest.mark.django_db
def test_logout_cannot_be_triggered_by_a_get(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER)
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), token)
        assert client.get(reverse("customer_account_logout")).status_code == 405
        assert services.session_account(token) is not None


# --- Redirects --------------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_account_redirect_can_be_pointed_off_site(public_catalog):
    """Every redirect target is a reversed name, never anything from the request."""
    with public_account_runtime():
        token = sign_in(MAX_USER)
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), token)
        evil = "https://evil.example/steal"
        for name, data in [
            ("customer_account_logout", {}),
            ("customer_account_profile", {"display_name": "Имя"}),
            ("customer_account_telegram_unlink", {}),
        ]:
            response = client.post(
                reverse(name) + f"?next={evil}", {**data, "next": evil, "redirect_to": evil}
            )
            assert response.status_code == 302
            assert response["Location"].startswith("/"), name
            assert "evil.example" not in response["Location"], name
            client.cookies[web_session.ACCOUNT_COOKIE] = token


@pytest.mark.django_db
def test_the_login_redirect_target_is_always_the_local_login_page(public_catalog):
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        response = client.get(reverse("customer_account_home") + "?next=https://evil.example")
        assert response["Location"] == reverse("customer_account_login")


# --- Rate limit -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_login_attempts_are_rate_limited_per_client(public_catalog):
    from django.core.cache import cache

    cache.clear()
    with public_account_runtime(CUSTOMER_LOGIN_RATE_LIMIT=3):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        statuses = [
            client.post(reverse("customer_account_login_max")).status_code for _ in range(6)
        ]
        assert statuses == [302] * 6  # always a redirect, never an error page
        assert CustomerLoginAttempt.objects.count() <= 4
    cache.clear()


# --- Consent evidence -------------------------------------------------------------------------


@pytest.mark.django_db
def test_no_consent_version_means_no_checkbox_and_no_evidence_row(public_catalog):
    with public_account_runtime(CUSTOMER_ACCOUNT_CONSENT_VERSION=""):
        sign_in(MAX_USER)
        assert not CustomerConsent.objects.exists()


@pytest.mark.django_db
def test_a_published_consent_version_is_required_and_recorded(public_catalog):
    with public_account_runtime(CUSTOMER_ACCOUNT_CONSENT_VERSION="pdn-2026-09-20"):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        attempt = start_login_attempt()
        reply = services.provider_confirmed(
            provider=Provider.MAX, token=attempt.token,
            provider_user_id=MAX_USER, chat_id=1, display_name="Клиент",
        )
        client.cookies[web_session.LOGIN_COOKIE] = (
            f"{attempt.browser_secret}.{attempt.token}"
        )
        # Without the checkbox the code is not even tried.
        refused = client.post(reverse("customer_account_login_code"), {"code": reply.code})
        assert refused.status_code == 302
        assert not CustomerAccount.objects.exists()

        accepted = client.post(
            reverse("customer_account_login_code"),
            {"code": reply.code, "consent_account": "1"},
        )
        assert accepted.status_code == 302
        consent = CustomerConsent.objects.get()
        assert consent.document_version == "pdn-2026-09-20"
        assert consent.purpose == CustomerConsent.Purpose.ACCOUNT
        assert consent.withdrawn_at is None


@pytest.mark.django_db
def test_consent_can_be_withdrawn_and_stops_counting(public_catalog):
    with public_account_runtime(CUSTOMER_ACCOUNT_CONSENT_VERSION="pdn-1"):
        sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        services.give_consent(account, CustomerConsent.Purpose.ACCOUNT, action="test")
        assert services.has_consent(account, CustomerConsent.Purpose.ACCOUNT)
        services.withdraw_consent(account, CustomerConsent.Purpose.ACCOUNT)
        assert not services.has_consent(account, CustomerConsent.Purpose.ACCOUNT)


@pytest.mark.django_db
def test_a_new_consent_version_invalidates_the_old_evidence(public_catalog):
    with public_account_runtime(CUSTOMER_ACCOUNT_CONSENT_VERSION="pdn-1"):
        sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        services.give_consent(account, CustomerConsent.Purpose.ACCOUNT, action="test")
        assert services.has_consent(account, CustomerConsent.Purpose.ACCOUNT)
    with public_account_runtime(CUSTOMER_ACCOUNT_CONSENT_VERSION="pdn-2"):
        assert not services.has_consent(account, CustomerConsent.Purpose.ACCOUNT)


# --- Profile ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_display_name_is_trimmed_bounded_and_never_empty(public_catalog):
    with public_account_runtime():
        sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        services.update_display_name(account, "   Пётр   Иванов  ")
        account.refresh_from_db()
        assert account.display_name == "Пётр Иванов"

        services.update_display_name(account, "я" * 300)
        account.refresh_from_db()
        assert len(account.display_name) == 120

        with pytest.raises(services.AccountError):
            services.update_display_name(account, "   ")


@pytest.mark.django_db
def test_the_profile_page_never_shows_a_raw_secret(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER)
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), token)
        body = client.get(reverse("customer_account_profile")).content.decode()
        assert token not in body
        assert tokens.digest(token) not in body


@pytest.mark.django_db
def test_the_account_code_is_a_display_handle_and_opens_nothing(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        assert account.code == str(account.public_id).split("-")[0].upper()
        # It is not a session token and not an address.
        assert services.session_account(account.code) is None
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), token)
        assert client.get(f"/account/requests/{account.code}/").status_code == 404
