"""MAX-only sign-in: the code flow, replay, nonce, concurrency and sessions.

Website authentication is MAX and nothing else (owner decision for V1). The
proof travels messenger → person → browser: the bot sends a one-time code to
the MAX user the messenger's own server identified, and that code is typed
into the SAME browser that started the attempt. A link forwarded to a victim
therefore cannot sign an attacker in, and a code alone is useless elsewhere.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest import mock

import pytest
from django.db import connection
from django.test import override_settings
from django.utils import timezone

from apps.customer_accounts import services, tokens
from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerAccountEvent,
    CustomerIdentity,
    CustomerLoginAttempt,
    CustomerSession,
    Provider,
)
from tests.customer_account_support import account_on, sign_in, start_login_attempt

MAX_USER = 8100001


def _confirm(attempt, user_id=MAX_USER, *, provider=Provider.MAX, name="Пётр"):
    return services.provider_confirmed(
        provider=provider,
        token=attempt.token,
        provider_user_id=user_id,
        chat_id=user_id + 1,
        display_name=name,
    )


# --- The only way in -----------------------------------------------------------------------


@pytest.mark.django_db
def test_max_is_the_only_website_login_and_telegram_only_links():
    with account_on():
        assert services.login_enabled(Provider.MAX)
        assert not services.login_enabled(Provider.TELEGRAM)
        assert services.link_enabled(Provider.TELEGRAM)
        assert not services.link_enabled(Provider.MAX)
        assert services.LOGIN_PROVIDERS == frozenset({Provider.MAX})


@pytest.mark.django_db
def test_telegram_can_never_open_a_login_attempt():
    with account_on():
        with pytest.raises(services.AccountError):
            start_login_attempt(provider=Provider.TELEGRAM)
        assert not CustomerLoginAttempt.objects.exists()


@pytest.mark.django_db
def test_max_login_disabled_by_its_own_switch_refuses_even_with_the_feature_on():
    with account_on(CUSTOMER_AUTH_MAX_ENABLED=False):
        with pytest.raises(services.AccountError):
            start_login_attempt()


# --- First login, returning login -----------------------------------------------------------


@pytest.mark.django_db
def test_first_login_creates_exactly_one_account_with_one_identity():
    with account_on():
        attempt = start_login_attempt()
        reply = _confirm(attempt)
        assert len(reply.code) == 6 and reply.code.isdigit()

        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        )
        assert completion.ok

        account = CustomerAccount.objects.get()
        assert account.display_name == "Пётр" and account.is_active
        identity = CustomerIdentity.objects.get()
        assert (identity.provider, identity.provider_user_id) == (Provider.MAX, MAX_USER)
        assert identity.account_id == account.pk
        assert CustomerSession.objects.filter(account=account, revoked_at=None).count() == 1
        kinds = set(
            CustomerAccountEvent.objects.filter(account=account).values_list("kind", flat=True)
        )
        assert {"created", "identity_linked", "login"} <= kinds


@pytest.mark.django_db
def test_returning_login_reuses_the_account_and_issues_a_second_session():
    with account_on():
        first = sign_in(MAX_USER, name="Пётр")
        second = sign_in(MAX_USER, name="Пётр Иванов")

        assert first != second
        assert CustomerAccount.objects.count() == 1
        assert CustomerIdentity.objects.count() == 1
        account = CustomerAccount.objects.get()
        assert services.session_account(first).pk == account.pk
        assert services.session_account(second).pk == account.pk
        assert account.last_login_at is not None


@pytest.mark.django_db
def test_a_different_max_user_gets_a_separate_account():
    with account_on():
        sign_in(MAX_USER)
        sign_in(MAX_USER + 500)
        assert CustomerAccount.objects.count() == 2
        assert CustomerIdentity.objects.count() == 2


@pytest.mark.django_db
def test_identical_names_never_merge_two_accounts():
    """Names, usernames and phone text are snapshots; only provider ids decide."""
    with account_on():
        sign_in(MAX_USER, name="Иван Иванов")
        sign_in(MAX_USER + 1, name="Иван Иванов")
        assert CustomerAccount.objects.count() == 2
        assert {a.display_name for a in CustomerAccount.objects.all()} == {"Иван Иванов"}


# --- Replay, nonce, expiry, lockout ---------------------------------------------------------


@pytest.mark.django_db
def test_a_completed_attempt_cannot_be_replayed():
    with account_on():
        attempt = start_login_attempt()
        reply = _confirm(attempt)
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        ).ok

        replay = services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        )
        assert not replay.ok and replay.outcome == services.Outcome.INVALID
        assert CustomerSession.objects.count() == 1


@pytest.mark.django_db
def test_the_code_is_bound_to_its_own_attempt():
    """The same six digits mean nothing in a second, parallel attempt."""
    with account_on():
        mine = start_login_attempt(client_key="a")
        theirs = start_login_attempt(client_key="b")
        code = _confirm(mine).code
        _confirm(theirs, MAX_USER + 9)

        stolen = services.complete_attempt(browser_secret=theirs.browser_secret, code=code)
        assert not stolen.ok and stolen.outcome == services.Outcome.WRONG_CODE


@pytest.mark.django_db
def test_the_code_alone_is_useless_without_the_browser_secret():
    """A login link forwarded to a victim cannot sign the forwarder in."""
    with account_on():
        attempt = start_login_attempt()
        reply = _confirm(attempt)

        assert not services.complete_attempt(browser_secret="", code=reply.code).ok
        assert not services.complete_attempt(
            browser_secret=tokens.new_token(), code=reply.code
        ).ok
        assert not CustomerSession.objects.exists()


@pytest.mark.django_db
def test_a_second_max_user_cannot_take_over_someone_elses_link():
    with account_on():
        attempt = start_login_attempt()
        first = _confirm(attempt, MAX_USER)
        assert first.code

        hijack = _confirm(attempt, MAX_USER + 77)
        assert hijack.code == ""
        assert hijack.text == services.ATTEMPT_UNAVAILABLE_TEXT

        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=first.code
        ).ok
        assert CustomerIdentity.objects.get().provider_user_id == MAX_USER


@pytest.mark.django_db
def test_an_expired_attempt_fails_closed():
    with account_on():
        attempt = start_login_attempt()
        reply = _confirm(attempt)
        CustomerLoginAttempt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))

        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        )
        assert completion.outcome == services.Outcome.EXPIRED
        assert not CustomerSession.objects.exists()
        assert CustomerLoginAttempt.objects.get().status == CustomerLoginAttempt.Status.FAILED


@pytest.mark.django_db
def test_an_expired_attempt_cannot_be_revived_by_the_bot():
    with account_on():
        attempt = start_login_attempt()
        CustomerLoginAttempt.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        assert _confirm(attempt).code == ""


@pytest.mark.django_db
def test_wrong_codes_lock_the_attempt_and_never_create_an_account():
    with override_settings(CUSTOMER_LOGIN_CODE_TRIES=3), account_on():
        attempt = start_login_attempt()
        reply = _confirm(attempt)
        wrong = "000000" if reply.code != "000000" else "111111"

        outcomes = [
            services.complete_attempt(
                browser_secret=attempt.browser_secret, code=wrong
            ).outcome
            for _ in range(3)
        ]
        assert outcomes == [
            services.Outcome.WRONG_CODE,
            services.Outcome.WRONG_CODE,
            services.Outcome.LOCKED,
        ]
        # The real code no longer helps once the attempt is locked.
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        ).outcome == services.Outcome.INVALID
        assert not CustomerAccount.objects.exists()


@pytest.mark.django_db
def test_a_code_may_be_resent_only_a_few_times():
    with account_on():
        attempt = start_login_attempt()
        codes = [_confirm(attempt).code for _ in range(services.MAX_CODES_PER_ATTEMPT)]
        assert all(codes)
        assert _confirm(attempt).code == ""
        # Only the newest code works; the earlier ones are dead.
        for stale in codes[:-1]:
            assert not services.complete_attempt(
                browser_secret=attempt.browser_secret, code=stale
            ).ok
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=codes[-1]
        ).ok


@pytest.mark.django_db
def test_a_code_that_was_never_sent_cannot_complete_an_attempt():
    with account_on():
        attempt = start_login_attempt()
        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret, code="123456"
        )
        assert completion.outcome == services.Outcome.NOT_READY


@pytest.mark.django_db
def test_a_malformed_code_is_refused_without_touching_the_account():
    with account_on():
        attempt = start_login_attempt()
        _confirm(attempt)
        for bad in ["", "   ", "12345", "1234567", "abcdef", "12345a", None, "١٢٣٤٥٦"]:
            assert not services.complete_attempt(
                browser_secret=attempt.browser_secret, code=bad
            ).ok
        assert not CustomerAccount.objects.exists()


@pytest.mark.django_db
def test_a_bot_payload_that_is_not_ours_is_ignored():
    for payload in [None, "", "req_abc", "acc_", "acc_short", "acc_" + "x" * 200, 12345,
                    "acc_" + "a" * 30 + "/../"]:
        assert tokens.token_from_payload(payload) is None
    assert tokens.token_from_payload(tokens.start_payload("A" * 43)) == "A" * 43


@pytest.mark.django_db
def test_a_provider_id_that_is_not_a_positive_integer_is_refused():
    with account_on():
        attempt = start_login_attempt()
        for bad in [0, -1, "7", None, True]:
            reply = services.provider_confirmed(
                provider=Provider.MAX, token=attempt.token,
                provider_user_id=bad, chat_id=1, display_name="x",
            )
            assert reply.code == ""


# --- Concurrency ------------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_a_lost_identity_race_falls_back_to_the_winning_account():
    """The unique constraint, not the pre-check, is what decides the winner.

    Simulates the real race on any backend: the winner's identity is already
    committed, but this call's pre-check still sees nothing — exactly what two
    workers interleaving looks like. The INSERT must then lose to the database
    constraint, and the loser must adopt the winner's account and roll its own
    back rather than creating a duplicate.
    """
    with account_on():
        winner = CustomerAccount.objects.create(display_name="Первый")
        CustomerIdentity.objects.create(
            account=winner,
            provider=Provider.MAX,
            provider_user_id=MAX_USER,
            display_name="",
            verified_at=timezone.now(),
        )
        blind = {"on": True}
        real_filter = CustomerIdentity.objects.filter
        real_create = CustomerIdentity.objects.create

        def blind_filter(*args, **kwargs):
            return CustomerIdentity.objects.none() if blind["on"] else real_filter(*args, **kwargs)

        def racing_create(**kwargs):
            blind["on"] = False  # the race is over; the INSERT below will lose
            return real_create(**kwargs)

        with mock.patch.object(CustomerIdentity.objects, "filter", blind_filter), \
                mock.patch.object(CustomerIdentity.objects, "create", racing_create):
            account = services._create_account_with_identity(Provider.MAX, MAX_USER, "Второй")

        assert account.pk == winner.pk
        assert CustomerIdentity.objects.count() == 1
        assert CustomerAccount.objects.count() == 1  # the loser's account was rolled back


@pytest.mark.skipif(
    connection.vendor != "postgresql", reason="SQLite cannot write from parallel connections"
)
@pytest.mark.django_db(transaction=True)
def test_concurrent_first_logins_of_one_max_user_create_one_account():
    """Four browsers, one MAX user, at the same instant: still one account."""
    with account_on():
        attempts = [start_login_attempt(client_key=f"c{i}") for i in range(4)]
        codes = [_confirm(a).code for a in attempts]

        def finish(pair):
            attempt, code = pair
            try:
                return services.complete_attempt(
                    browser_secret=attempt.browser_secret, code=code
                )
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(finish, zip(attempts, codes)))

        assert all(r.ok for r in results)
        assert CustomerAccount.objects.count() == 1
        assert CustomerIdentity.objects.count() == 1
        assert len({r.account_id for r in results}) == 1
        assert CustomerSession.objects.count() == 4


@pytest.mark.django_db(transaction=True)
def test_one_identity_can_never_belong_to_two_accounts():
    with account_on():
        first = CustomerAccount.objects.create(display_name="A")
        second = CustomerAccount.objects.create(display_name="B")
        services._attach_identity(first, Provider.TELEGRAM, 4242, "x")
        with pytest.raises(services.IdentityConflict):
            services._attach_identity(second, Provider.TELEGRAM, 4242, "x")
        assert CustomerIdentity.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_an_account_can_never_hold_two_identities_of_one_provider():
    with account_on():
        account = CustomerAccount.objects.create(display_name="A")
        services._attach_identity(account, Provider.MAX, 111, "x")
        with pytest.raises(services.IdentityConflict):
            services._attach_identity(account, Provider.MAX, 222, "x")


# --- Sessions -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_every_sign_in_mints_a_fresh_token_so_a_planted_one_is_never_promoted():
    """No session fixation: the token that ends up signed in is generated at
    completion, never taken from the browser."""
    with account_on():
        planted = tokens.new_token()
        token = sign_in(MAX_USER)
        assert token != planted
        assert services.session_account(planted) is None
        assert CustomerSession.objects.filter(token_hash=tokens.digest(planted)).count() == 0


@pytest.mark.django_db
def test_only_the_digest_of_a_session_token_is_stored():
    with account_on():
        token = sign_in(MAX_USER)
        stored = CustomerSession.objects.get().token_hash
        assert stored == tokens.digest(token) and token not in stored


@pytest.mark.django_db
def test_logout_revokes_only_that_browser():
    with account_on():
        phone = sign_in(MAX_USER)
        laptop = sign_in(MAX_USER)

        services.revoke_session(phone)
        assert services.session_account(phone) is None
        assert services.session_account(laptop) is not None
        assert CustomerAccountEvent.objects.filter(kind="logout").count() == 1


@pytest.mark.django_db
def test_logout_is_idempotent_and_ignores_rubbish():
    with account_on():
        token = sign_in(MAX_USER)
        services.revoke_session(token)
        services.revoke_session(token)
        services.revoke_session("")
        services.revoke_session("not-a-token")
        assert CustomerAccountEvent.objects.filter(kind="logout").count() == 1


@pytest.mark.django_db
def test_an_expired_session_stops_working():
    with account_on():
        token = sign_in(MAX_USER)
        CustomerSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        assert services.session_account(token) is None


@pytest.mark.django_db
def test_a_forged_or_oversized_session_token_resolves_to_nobody():
    with account_on():
        sign_in(MAX_USER)
        for bad in ["", "x", tokens.new_token(), "a" * 129, "a" * 5000]:
            assert services.session_account(bad) is None


@pytest.mark.django_db
def test_deactivating_an_account_kills_every_session_and_refuses_new_logins():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        services.deactivate_account(account)

        assert services.session_account(token) is None
        attempt = start_login_attempt()
        reply = _confirm(attempt)
        completion = services.complete_attempt(
            browser_secret=attempt.browser_secret, code=reply.code
        )
        assert completion.outcome == services.Outcome.DEACTIVATED
        assert services.identity_account(Provider.MAX, MAX_USER) is None


@pytest.mark.django_db
def test_deactivation_is_idempotent_and_keeps_the_business_records():
    with account_on():
        sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        services.deactivate_account(account)
        services.deactivate_account(account)
        assert CustomerAccountEvent.objects.filter(kind="deactivated").count() == 1
        assert CustomerIdentity.objects.count() == 1
