"""The activation gate: everything is off until the owner and legal say so.

``CUSTOMER_ACCOUNT_ENABLED`` defaults to False. With it off the account simply
does not exist on the public site: no pages, no links, no account creation at
messenger handoff, no request ownership — and every existing PRO-STOR journey
(search, part pages, cart, anonymous request, MAX and Telegram messaging)
behaves exactly as it does today.
"""

import pytest
from django.conf import settings
from django.test import Client
from django.urls import reverse

from apps.customer_accounts import services
from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerIdentity,
    CustomerLoginAttempt,
    Provider,
)
from apps.customer_requests.models import CustomerRequest
from tests.customer_account_support import (
    ACCOUNT_OFF,
    link_max_conversation,
    make_request,
    public_account_runtime,
    sign_in,
)
from tests.public_catalog_support import PUBLIC_HOST, public_runtime_settings

ACCOUNT_URL_NAMES = [
    "customer_account_home",
    "customer_account_login",
    "customer_account_login_code",
    "customer_account_requests",
    "customer_account_purchases",
    "customer_account_messengers",
    "customer_account_telegram_code",
    "customer_account_profile",
]
POST_ONLY_NAMES = [
    "customer_account_login_max",
    "customer_account_logout",
    "customer_account_telegram_link",
    "customer_account_telegram_unlink",
]
MAX_USER = 8600001


# --- The default ------------------------------------------------------------------------------


def test_every_account_switch_defaults_to_off():
    assert settings.CUSTOMER_ACCOUNT_ENABLED is False
    assert settings.CUSTOMER_AUTH_MAX_ENABLED is False


@pytest.mark.django_db
def test_the_services_refuse_while_the_flag_is_off():
    with public_runtime_settings(**ACCOUNT_OFF):
        assert not services.account_enabled()
        assert not services.login_enabled(Provider.MAX)
        assert not services.link_enabled(Provider.TELEGRAM)


# --- Disabled: the account does not exist -----------------------------------------------------


@pytest.mark.django_db
def test_every_account_page_is_404_while_disabled(public_catalog):
    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for name in ACCOUNT_URL_NAMES:
            response = client.get(reverse(name))
            assert response.status_code == 404, name
        for name in POST_ONLY_NAMES:
            response = client.post(reverse(name))
            assert response.status_code == 404, name


@pytest.mark.django_db
def test_no_account_page_ever_answers_500_while_disabled(public_catalog):
    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for path in [
            "/account/", "/account/login/", "/account/login/code/",
            "/account/requests/", "/account/requests/00000000-0000-0000-0000-000000000000/",
            "/account/purchases/", "/account/purchases/1/", "/account/purchases/1/reorder/",
            "/account/messengers/", "/account/profile/", "/account/nope/",
        ]:
            assert client.get(path).status_code in (404, 301), path


@pytest.mark.django_db
def test_no_login_or_account_cta_is_exposed_while_disabled(public_catalog):
    public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for path in ["/", "/search/?q=piston", "/cart/"]:
            body = client.get(path).content.decode()
            assert "/account/" not in body, path
            assert "Мой кабинет" not in body, path
            assert ">Войти<" not in body, path


@pytest.mark.django_db
def test_a_stale_account_cookie_changes_nothing_while_disabled(public_catalog):
    from apps.customer_accounts import tokens, web_session

    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        client.cookies[web_session.ACCOUNT_COOKIE] = tokens.new_token()
        body = client.get("/").content.decode()
        assert client.get("/").status_code == 200
        assert "Мой кабинет" not in body and "/account/" not in body


@pytest.mark.django_db
def test_messenger_handoff_keeps_web_account_disabled_but_records_identity(public_catalog):
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    public_catalog.stock(part, "5")
    with public_runtime_settings(**ACCOUNT_OFF):
        request = make_request(public_catalog, part, key="disabled-handoff")
        link_max_conversation(request, MAX_USER)
        services.attach_request_at_handoff(request, Provider.MAX, MAX_USER, "Клиент")

        request.refresh_from_db()
        assert request.customer_account_id is not None
        assert CustomerAccount.objects.count() == 1
        assert CustomerIdentity.objects.filter(
            provider=Provider.MAX, provider_user_id=MAX_USER
        ).exists()
        assert services.account_enabled() is False


@pytest.mark.django_db
def test_the_anonymous_request_journey_is_untouched_while_disabled(public_catalog):
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    public_catalog.stock(part, "5")
    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        assert client.get("/").status_code == 200
        assert client.get(f"/parts/{part.public_id}/").status_code == 200
        assert client.post(f"/cart/{part.public_id}/add/", {"quantity": "2"}).status_code == 302
        assert client.get("/cart/").status_code == 200
        assert client.get("/request/").status_code == 200

        request = make_request(public_catalog, part, key="anon-journey")
        assert CustomerRequest.objects.filter(pk=request.pk).exists()
        assert request.customer_account_id is None


# --- Enabled: it exists, and nothing else changes ---------------------------------------------


@pytest.mark.django_db
def test_every_account_page_answers_while_enabled(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER, name="Клиент")
        client = Client(HTTP_HOST=PUBLIC_HOST)
        assert client.get(reverse("customer_account_login")).status_code == 200

        from apps.customer_accounts import web_session

        client.cookies[web_session.ACCOUNT_COOKIE] = token
        for name in ["customer_account_home", "customer_account_requests",
                     "customer_account_purchases", "customer_account_messengers",
                     "customer_account_profile"]:
            assert client.get(reverse(name)).status_code == 200, name


@pytest.mark.django_db
def test_the_header_offers_login_then_the_account_while_enabled(public_catalog):
    public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        body = client.get("/").content.decode()
        assert "Войти" in body and reverse("customer_account_login") in body
        assert "Мой кабинет" not in body

        from apps.customer_accounts import web_session

        client.cookies[web_session.ACCOUNT_COOKIE] = sign_in(MAX_USER)
        body = client.get("/").content.decode()
        assert "Мой кабинет" in body and reverse("customer_account_home") in body


@pytest.mark.django_db
def test_the_public_catalog_journey_is_unchanged_while_enabled(public_catalog):
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    public_catalog.stock(part, "5")
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for path in ["/", "/search/?q=piston", f"/parts/{part.public_id}/", "/cart/",
                     "/robots.txt", "/sitemap.xml", "/healthz/"]:
            assert client.get(path).status_code == 200, path
        # The anonymous request form still opens from a filled cart, as today.
        client.post(f"/cart/{part.public_id}/add/", {"quantity": "1"})
        assert client.get("/request/").status_code == 200


@pytest.mark.django_db
def test_turning_the_flag_off_again_hides_existing_accounts_safely(public_catalog):
    with public_account_runtime():
        token = sign_in(MAX_USER)
        assert CustomerAccount.objects.count() == 1
    with public_runtime_settings(**ACCOUNT_OFF):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        from apps.customer_accounts import web_session

        client.cookies[web_session.ACCOUNT_COOKIE] = token
        assert client.get(reverse("customer_account_home")).status_code == 404
        assert client.get("/").status_code == 200
        # The rows stay; nothing is destroyed by flipping the switch.
        assert CustomerAccount.objects.count() == 1


@pytest.mark.django_db
def test_the_max_login_switch_gates_the_button_independently(public_catalog):
    with public_account_runtime(CUSTOMER_AUTH_MAX_ENABLED=False):
        client = Client(HTTP_HOST=PUBLIC_HOST)
        page = client.get(reverse("customer_account_login"))
        assert page.status_code == 200
        assert "Продолжить через MAX" not in page.content.decode()
        posted = client.post(reverse("customer_account_login_max"))
        assert posted.status_code == 302
        assert not CustomerLoginAttempt.objects.exists()


@pytest.mark.django_db
def test_the_login_page_says_the_account_is_optional_and_needs_no_password(public_catalog):
    with public_account_runtime():
        body = Client(HTTP_HOST=PUBLIC_HOST).get(
            reverse("customer_account_login")
        ).content.decode()
        assert "Продолжить через MAX" in body
        # The account is created automatically: no password, no registration form.
        assert "без пароля и регистрации" in body
        # Nothing to fill in: the only input in the sign-in block is the CSRF
        # token, and there is no password field anywhere on the page.
        auth = body[body.index('class="account-auth"'):]
        assert auth.count("<input") == 1 and "csrfmiddlewaretoken" in auth
        assert 'type="password"' not in body
        # The account stays optional, and the page says so.
        assert "без аккаунта" in body
