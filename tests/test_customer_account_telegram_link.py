"""Telegram is a messaging channel that may be LINKED, never a way in.

A Telegram identity reaches an account only after a customer who already
signed in through MAX asked for it in their own live session and typed the
code Telegram delivered. Telegram alone creates nothing and signs in nobody.
"""

import pytest

from apps.customer_accounts import messenger_hooks, services
from apps.customer_accounts.models import (
    CustomerAccount,
    CustomerIdentity,
    CustomerLoginAttempt,
    Provider,
)
from tests.customer_account_support import account_on, sign_in, start_login_attempt

MAX_USER = 8200001
TG_USER = 8200500


def _confirm(attempt, provider, user_id, name="Клиент"):
    return services.provider_confirmed(
        provider=provider, token=attempt.token, provider_user_id=user_id,
        chat_id=user_id + 1, display_name=name,
    )


def _link(session_token, account, user_id=TG_USER):
    attempt = start_login_attempt(
        purpose=CustomerLoginAttempt.Purpose.LINK, provider=Provider.TELEGRAM, account=account
    )
    reply = _confirm(attempt, Provider.TELEGRAM, user_id)
    return attempt, services.complete_attempt(
        browser_secret=attempt.browser_secret, code=reply.code, session_token=session_token
    )


# --- Never a login ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_telegram_start_never_creates_an_account():
    with account_on():
        assert services.identity_account(Provider.TELEGRAM, TG_USER) is None
        assert not CustomerAccount.objects.exists()
        assert not CustomerIdentity.objects.exists()


@pytest.mark.django_db
def test_a_telegram_link_attempt_without_a_live_session_links_nothing():
    with account_on():
        account = CustomerAccount.objects.create(display_name="Клиент")
        _attempt, completion = _link("not-a-session", account)
        assert not completion.ok
        assert completion.outcome == services.Outcome.INVALID
        assert not CustomerIdentity.objects.filter(provider=Provider.TELEGRAM).exists()


@pytest.mark.django_db
def test_a_link_attempt_cannot_be_completed_from_someone_elses_session():
    """Account B's live session must not finish account A's link attempt."""
    with account_on():
        sign_in(MAX_USER)
        victim = CustomerAccount.objects.get()
        attacker_token = sign_in(MAX_USER + 1)

        _attempt, completion = _link(attacker_token, victim)
        assert not completion.ok and completion.outcome == services.Outcome.INVALID
        assert not CustomerIdentity.objects.filter(provider=Provider.TELEGRAM).exists()


@pytest.mark.django_db
def test_a_link_attempt_needs_an_account_and_the_feature_on():
    with account_on():
        with pytest.raises(services.AccountError):
            start_login_attempt(
                purpose=CustomerLoginAttempt.Purpose.LINK,
                provider=Provider.TELEGRAM,
                account=None,
            )
    with account_on(CUSTOMER_ACCOUNT_ENABLED=False):
        account = CustomerAccount.objects.create(display_name="x")
        with pytest.raises(services.AccountError):
            start_login_attempt(
                purpose=CustomerLoginAttempt.Purpose.LINK,
                provider=Provider.TELEGRAM,
                account=account,
            )


@pytest.mark.django_db
def test_a_deactivated_account_cannot_link_a_messenger():
    with account_on():
        sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        services.deactivate_account(account)
        account.refresh_from_db()
        with pytest.raises(services.AccountError):
            start_login_attempt(
                purpose=CustomerLoginAttempt.Purpose.LINK,
                provider=Provider.TELEGRAM,
                account=account,
            )


# --- The happy path ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_a_signed_in_customer_links_telegram_with_the_code_it_sent():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        _attempt, completion = _link(token, account)

        assert completion.ok
        identity = CustomerIdentity.objects.get(provider=Provider.TELEGRAM)
        assert identity.account_id == account.pk and identity.provider_user_id == TG_USER
        # Linking never mints a new website session.
        assert services.session_account(token).pk == account.pk
        assert CustomerAccount.objects.count() == 1


@pytest.mark.django_db
def test_a_linked_telegram_identity_then_resolves_at_handoff():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        _link(token, account)
        assert services.identity_account(Provider.TELEGRAM, TG_USER).pk == account.pk


# --- Conflicts and unlink ---------------------------------------------------------------------


@pytest.mark.django_db
def test_a_telegram_identity_already_on_another_account_is_refused():
    with account_on():
        first_token = sign_in(MAX_USER)
        first = CustomerAccount.objects.get(identities__provider_user_id=MAX_USER)
        _link(first_token, first)

        second_token = sign_in(MAX_USER + 1)
        second = CustomerAccount.objects.get(identities__provider_user_id=MAX_USER + 1)
        attempt = start_login_attempt(
            purpose=CustomerLoginAttempt.Purpose.LINK,
            provider=Provider.TELEGRAM,
            account=second,
        )
        reply = _confirm(attempt, Provider.TELEGRAM, TG_USER)
        # The bot itself refuses before any code is ever sent.
        assert reply.code == "" and reply.text == services.IDENTITY_TAKEN_TEXT
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code="123456", session_token=second_token
        ).outcome == services.Outcome.INVALID
        assert CustomerIdentity.objects.get(provider=Provider.TELEGRAM).account_id == first.pk


@pytest.mark.django_db
def test_a_second_telegram_for_one_account_is_refused():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        _link(token, account, TG_USER)
        _attempt, completion = _link(token, account, TG_USER + 5)
        assert completion.outcome == services.Outcome.CONFLICT
        assert CustomerIdentity.objects.filter(provider=Provider.TELEGRAM).count() == 1


@pytest.mark.django_db
def test_relinking_the_same_telegram_is_harmless():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        _link(token, account)
        _attempt, again = _link(token, account)
        assert again.ok
        assert CustomerIdentity.objects.filter(provider=Provider.TELEGRAM).count() == 1


@pytest.mark.django_db
def test_telegram_can_be_unlinked_and_max_can_never_be():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        _link(token, account)

        services.unlink_identity(account, Provider.TELEGRAM)
        assert not CustomerIdentity.objects.filter(provider=Provider.TELEGRAM).exists()
        # Unlinking twice is a no-op, not an error.
        services.unlink_identity(account, Provider.TELEGRAM)

        with pytest.raises(services.AccountError):
            services.unlink_identity(account, Provider.MAX)
        assert CustomerIdentity.objects.filter(provider=Provider.MAX).count() == 1


@pytest.mark.django_db
def test_an_unlinked_telegram_can_be_linked_to_a_different_account_afterwards():
    with account_on():
        first_token = sign_in(MAX_USER)
        first = CustomerAccount.objects.get(identities__provider_user_id=MAX_USER)
        _link(first_token, first)
        services.unlink_identity(first, Provider.TELEGRAM)

        second_token = sign_in(MAX_USER + 1)
        second = CustomerAccount.objects.get(identities__provider_user_id=MAX_USER + 1)
        _attempt, completion = _link(second_token, second)
        assert completion.ok
        assert CustomerIdentity.objects.get(provider=Provider.TELEGRAM).account_id == second.pk


# --- The bot hooks ----------------------------------------------------------------------------


@pytest.mark.django_db
def test_the_telegram_bot_hook_ignores_a_payload_that_is_not_an_account_link():
    with account_on():
        assert messenger_hooks.telegram_start(
            argument="some-request-token", user_id=TG_USER, chat_id=1, user={}
        ) is None


@pytest.mark.django_db
def test_the_telegram_bot_hook_answers_an_account_link_payload():
    with account_on():
        token = sign_in(MAX_USER)
        account = CustomerAccount.objects.get()
        attempt = start_login_attempt(
            purpose=CustomerLoginAttempt.Purpose.LINK,
            provider=Provider.TELEGRAM,
            account=account,
        )
        text = messenger_hooks.telegram_start(
            argument=attempt.start_payload,
            user_id=TG_USER,
            chat_id=TG_USER + 1,
            user={"first_name": "Тест", "last_name": "Клиент"},
        )
        assert text is not None and "код" in text.lower()
        code = "".join(ch for ch in text if ch.isdigit())[:6]
        assert services.complete_attempt(
            browser_secret=attempt.browser_secret, code=code, session_token=token
        ).ok


@pytest.mark.django_db
def test_display_name_is_only_a_snapshot_and_is_bounded():
    assert messenger_hooks.display_name({"first_name": "А", "last_name": "Б"}) == "А Б"
    assert messenger_hooks.display_name({"name": "Только имя"}) == "Только имя"
    assert messenger_hooks.display_name(None) == ""
    assert len(messenger_hooks.display_name({"first_name": "я" * 400})) == 160
