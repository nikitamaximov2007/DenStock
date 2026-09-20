"""Ownership: what one account may reach, and what it may never reach.

Ownership is never taken from a URL. A request is found by its opaque
``public_id`` AMONG THE ACCOUNT'S OWN requests, a purchase by its number among
the account's own completed sales. A human request number is display text.
Anything that is not the account's own is 404 — the same answer as a row that
does not exist, so nothing is learned by probing.
"""

import uuid

import pytest
from django.test import Client
from django.urls import reverse

from apps.customer_accounts import history, services
from apps.customer_accounts.models import CustomerAccount, Provider
from apps.customer_requests.models import CustomerRequest
from tests.customer_account_support import (
    as_account,
    bound,
    link_customer_card,
    link_max_conversation,
    link_telegram_conversation,
    make_customer,
    make_request,
    make_sale,
    public_account_runtime,
    sign_in,
)
from tests.public_catalog_support import PUBLIC_HOST

ALICE_MAX = 8300001
BOB_MAX = 8300002


@pytest.fixture
def two_customers(public_catalog):
    """Two signed-in accounts, each with one owned request and one purchase."""
    part = public_catalog.part("PISTON ASSY", article="420892388", price="1000")
    lot = public_catalog.stock(part, "10")
    with public_account_runtime():
        alice_token = sign_in(ALICE_MAX, name="Алиса")
        bob_token = sign_in(BOB_MAX, name="Борис")
        alice = CustomerAccount.objects.get(identities__provider_user_id=ALICE_MAX)
        bob = CustomerAccount.objects.get(identities__provider_user_id=BOB_MAX)

        alice_request = make_request(public_catalog, part, name="Алиса", key="alice-1")
        link_max_conversation(alice_request, ALICE_MAX)
        services.claim_proven_requests(alice, Provider.MAX, ALICE_MAX)

        bob_request = make_request(public_catalog, part, name="Борис", key="bob-1")
        link_max_conversation(bob_request, BOB_MAX)
        services.claim_proven_requests(bob, Provider.MAX, BOB_MAX)

        alice_customer = make_customer("Алиса-карточка")
        bob_customer = make_customer("Борис-карточка")
        link_customer_card(alice, alice_customer, public_catalog.user)
        link_customer_card(bob, bob_customer, public_catalog.user)
        alice_sale = make_sale(alice_customer, part, lot=lot, unit_price="1000")
        bob_sale = make_sale(bob_customer, part, lot=lot, unit_price="2000")

        yield {
            "part": part,
            "alice": alice, "bob": bob,
            "alice_token": alice_token, "bob_token": bob_token,
            "alice_client": as_account(Client(HTTP_HOST=PUBLIC_HOST), alice_token),
            "bob_client": as_account(Client(HTTP_HOST=PUBLIC_HOST), bob_token),
            "alice_request": alice_request, "bob_request": bob_request,
            "alice_sale": alice_sale, "bob_sale": bob_sale,
            "alice_customer": alice_customer, "bob_customer": bob_customer,
        }


# --- Claiming ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_only_a_provably_owned_request_is_claimed(public_catalog, two_customers):
    alice, bob = two_customers["alice"], two_customers["bob"]
    two_customers["alice_request"].refresh_from_db()
    two_customers["bob_request"].refresh_from_db()
    assert two_customers["alice_request"].customer_account_id == alice.pk
    assert two_customers["bob_request"].customer_account_id == bob.pk


@pytest.mark.django_db
def test_an_anonymous_request_with_no_linked_conversation_is_never_claimed(public_catalog):
    with public_account_runtime():
        part = public_catalog.part("SPARK PLUG", article="A-1", price="500")
        public_catalog.stock(part, "10")
        orphan = make_request(public_catalog, part, name="Алиса", key="orphan")
        sign_in(ALICE_MAX, name="Алиса")
        account = CustomerAccount.objects.get()
        assert services.claim_proven_requests(account, Provider.MAX, ALICE_MAX) == 0
        orphan.refresh_from_db()
        assert orphan.customer_account_id is None


@pytest.mark.django_db
def test_a_matching_name_or_phone_never_claims_a_request(public_catalog):
    """Only the messenger conversation is evidence — never a name or phone."""
    with public_account_runtime():
        part = public_catalog.part("SPARK PLUG", article="A-1", price="500")
        public_catalog.stock(part, "10")
        theirs = make_request(
            public_catalog, part, name="Алиса", phone="+7 912 000-00-01", key="theirs"
        )
        link_max_conversation(theirs, BOB_MAX)
        sign_in(ALICE_MAX, name="Алиса")
        alice = CustomerAccount.objects.get()
        assert services.claim_proven_requests(alice, Provider.MAX, ALICE_MAX) == 0
        theirs.refresh_from_db()
        assert theirs.customer_account_id is None


@pytest.mark.django_db
def test_a_request_already_owned_is_never_taken_over(public_catalog, two_customers):
    """Even a genuine conversation cannot move a request between accounts."""
    alice_request = two_customers["alice_request"]
    link_telegram_conversation(alice_request, 8300900)
    with public_account_runtime():
        services.claim_proven_requests(two_customers["bob"], Provider.TELEGRAM, 8300900)
    alice_request.refresh_from_db()
    assert alice_request.customer_account_id == two_customers["alice"].pk


# --- Request IDOR -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_account_sees_only_its_own_requests(two_customers):
    with public_account_runtime(), bound(two_customers["alice_token"]):
        mine = history.account_requests(two_customers["alice"])
        assert [r.id for r in mine] == [two_customers["alice_request"].pk]


@pytest.mark.django_db
def test_another_accounts_request_is_not_found_by_its_opaque_id(two_customers):
    with public_account_runtime():
        with bound(two_customers["alice_token"]):
            assert history.account_request(
                two_customers["alice"], two_customers["bob_request"].public_id
            ) is None
        url = reverse(
            "customer_account_request", args=[two_customers["bob_request"].public_id]
        )
        assert two_customers["alice_client"].get(url).status_code == 404
        assert two_customers["bob_client"].get(url).status_code == 200


@pytest.mark.django_db
def test_a_human_request_number_opens_nothing(two_customers):
    """The human number is display text; it is not an address."""
    bob_request = two_customers["bob_request"]
    bob_request.refresh_from_db()
    with public_account_runtime():
        with bound(two_customers["alice_token"]):
            for guess in [str(bob_request.human_number or 1), "1", "2", "000001"]:
                assert history.account_request(two_customers["alice"], guess) is None
        for guess in [str(bob_request.human_number or 1), "1", "2", "000001"]:
            assert two_customers["alice_client"].get(
                f"/account/requests/{guess}/"
            ).status_code == 404


@pytest.mark.django_db
def test_sequential_and_random_id_guessing_finds_nothing(two_customers):
    with public_account_runtime(), bound(two_customers["alice_token"]):
        for _ in range(20):
            assert history.account_request(two_customers["alice"], uuid.uuid4()) is None
        for junk in ["", None, "not-a-uuid", 0, -1, "../../etc/passwd", "1 OR 1=1"]:
            assert history.account_request(two_customers["alice"], junk) is None


@pytest.mark.django_db
def test_a_messenger_identity_mismatch_never_opens_a_request(public_catalog, two_customers):
    """Signing in as a different MAX user does not inherit anything."""
    with public_account_runtime():
        stranger_token = sign_in(8300777, name="Чужой")
        stranger = CustomerAccount.objects.get(identities__provider_user_id=8300777)
        with bound(stranger_token):
            assert history.account_requests(stranger) == []
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), stranger_token)
        url = reverse(
            "customer_account_request", args=[two_customers["alice_request"].public_id]
        )
        assert client.get(url).status_code == 404


@pytest.mark.django_db
def test_account_pages_redirect_to_login_without_a_session(public_catalog):
    with public_account_runtime():
        client = Client(HTTP_HOST=PUBLIC_HOST)
        for name in [
            "customer_account_home",
            "customer_account_requests",
            "customer_account_purchases",
            "customer_account_messengers",
            "customer_account_profile",
        ]:
            response = client.get(reverse(name))
            assert response.status_code == 302, name
            assert response["Location"] == reverse("customer_account_login"), name


@pytest.mark.django_db
def test_a_forged_session_cookie_is_refused_and_cleared(public_catalog):
    from apps.customer_accounts import tokens, web_session

    with public_account_runtime():
        client = as_account(Client(HTTP_HOST=PUBLIC_HOST), tokens.new_token())
        response = client.get(reverse("customer_account_home"))
        assert response.status_code == 302
        assert response["Location"] == reverse("customer_account_login")
        assert response.cookies[web_session.ACCOUNT_COOKIE].value == ""


@pytest.mark.django_db
def test_a_revoked_session_stops_opening_pages(two_customers):
    with public_account_runtime():
        services.revoke_session(two_customers["alice_token"])
        response = two_customers["alice_client"].get(reverse("customer_account_home"))
        assert response.status_code == 302


# --- Sale ownership ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_an_account_sees_only_the_sales_of_its_own_linked_card(two_customers):
    with public_account_runtime(), bound(two_customers["alice_token"]):
        mine = history.account_purchases(two_customers["alice"])
        assert [p.id for p in mine] == [two_customers["alice_sale"].pk]
        assert history.account_purchase(
            two_customers["alice"], two_customers["bob_sale"].number
        ) is None


@pytest.mark.django_db
def test_a_foreign_sale_number_is_404_not_a_different_error(two_customers):
    with public_account_runtime():
        url = reverse("customer_account_purchase", args=[two_customers["bob_sale"].number])
        assert two_customers["alice_client"].get(url).status_code == 404
        assert two_customers["bob_client"].get(url).status_code == 200
        reorder_url = reverse(
            "customer_account_reorder", args=[two_customers["bob_sale"].number]
        )
        assert two_customers["alice_client"].get(reorder_url).status_code == 404


@pytest.mark.django_db
def test_an_account_with_no_linked_card_has_no_purchases(public_catalog, two_customers):
    with public_account_runtime():
        token = sign_in(8300888, name="Без карточки")
        account = CustomerAccount.objects.get(identities__provider_user_id=8300888)
        with bound(token):
            assert history.account_purchases(account) == []
        assert services.linked_customer_id(account) is None


@pytest.mark.django_db
def test_unlinking_the_card_hides_the_purchases_immediately(two_customers):
    with public_account_runtime():
        alice = two_customers["alice"]
        with bound(two_customers["alice_token"]):
            assert history.account_purchases(alice)
        services.unlink_customer(alice, by_user=None)
        with bound(two_customers["alice_token"]):
            assert history.account_purchases(alice) == []


@pytest.mark.django_db
def test_one_card_belongs_to_at_most_one_account(public_catalog, two_customers):
    with public_account_runtime():
        with pytest.raises(services.AccountError):
            link_customer_card(
                two_customers["bob"], two_customers["alice_customer"], public_catalog.user
            )
        with pytest.raises(services.AccountError):
            link_customer_card(
                two_customers["alice"], make_customer("Третья"), public_catalog.user
            )


@pytest.mark.django_db
def test_relinking_the_same_card_is_idempotent(public_catalog, two_customers):
    with public_account_runtime():
        again = link_customer_card(
            two_customers["alice"], two_customers["alice_customer"], public_catalog.user
        )
        assert again.account_id == two_customers["alice"].pk


@pytest.mark.django_db
def test_guessing_a_sale_number_by_url_finds_nothing(two_customers):
    with public_account_runtime():
        for guess in ["1", "0001", "П-1", "../", "%2e%2e", "a" * 40]:
            assert two_customers["alice_client"].get(
                f"/account/purchases/{guess}/"
            ).status_code in (404, 301)


@pytest.mark.django_db
def test_a_request_is_never_a_purchase(two_customers):
    """A CustomerRequest is a wish; purchase history comes from Sale only."""
    with public_account_runtime():
        alice = two_customers["alice"]
        with bound(two_customers["alice_token"]):
            assert len(history.account_requests(alice)) == 1
            purchases = history.account_purchases(alice)
        assert [p.id for p in purchases] == [two_customers["alice_sale"].pk]
        assert CustomerRequest.objects.filter(customer_account=alice).count() == 1
