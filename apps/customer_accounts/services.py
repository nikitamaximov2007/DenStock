"""The account rules, in one place.

Two kinds of caller:

* the bots and the webhook (internal role, full privileges) record what the
  messenger's OWN server said about a user — ``provider_confirmed`` and the
  handoff hook ``identity_account``;
* the public runtime (restricted role) creates attempts, completes them with a
  code, and reads its own session's data — ``create_attempt``,
  ``complete_attempt``, ``session_account``, ``revoke_session``.

On PostgreSQL the public runtime cannot write sessions, identities or requests
directly. The two privileged steps it needs — completing an attempt and
logging out — run as SECURITY DEFINER functions (``db_security``) that check the
same rules in the database, so a compromised public process cannot mint a
session without the code the messenger delivered. Everywhere else (SQLite in
tests, the internal role) the Python functions below do it, and the parity
tests run the same scenarios through both.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, connection, transaction
from django.utils import timezone

from . import tokens
from .models import (
    CustomerAccount,
    CustomerAccountCustomerLink,
    CustomerAccountEvent,
    CustomerConsent,
    CustomerIdentity,
    CustomerLoginAttempt,
    CustomerSession,
    Provider,
)

logger = logging.getLogger(__name__)

MAX_CODES_PER_ATTEMPT = 3


def _verified_user_id(value) -> int | None:
    """A provider identity, or None. ``bool`` is deliberately refused.

    ``isinstance(True, int)`` is true in Python, so a JSON ``true`` in a
    webhook payload would otherwise be read as MAX user 1 and could be
    attached to an account as a real identity.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class AccountError(Exception):
    """A customer-facing refusal. The message is safe to show."""


def account_enabled() -> bool:
    return bool(settings.CUSTOMER_ACCOUNT_ENABLED)


# V1 (owner decision): MAX is the only way to sign in and the only way an
# account comes into existence. Telegram is a messaging channel that a customer
# who already signed in through MAX may LINK; it never signs anyone in.
LOGIN_PROVIDERS = frozenset({Provider.MAX})
LINKABLE_PROVIDERS = frozenset({Provider.TELEGRAM})


def login_enabled(provider: str) -> bool:
    return (
        account_enabled()
        and provider in LOGIN_PROVIDERS
        and bool(settings.CUSTOMER_AUTH_MAX_ENABLED)
    )


def link_enabled(provider: str) -> bool:
    """Adding Telegram to an account that already signed in through MAX."""
    return account_enabled() and provider in LINKABLE_PROVIDERS


def _event(account, kind, actor_user=None, **detail) -> None:
    CustomerAccountEvent.objects.create(
        account=account, kind=kind, detail=detail, actor_user=actor_user
    )


# --- Attempts: created by the browser ----------------------------------------------------


@dataclass(frozen=True)
class NewAttempt:
    token: str
    browser_secret: str
    expires_at: object

    @property
    def start_payload(self) -> str:
        return tokens.start_payload(self.token)


def create_attempt(
    *, purpose: str, provider: str, client_key: str, account: CustomerAccount | None = None
) -> NewAttempt:
    """A fresh one-time attempt. The caller sets the browser cookie."""
    if purpose == CustomerLoginAttempt.Purpose.LOGIN and not login_enabled(provider):
        raise AccountError("Этот способ входа сейчас недоступен.")
    if purpose == CustomerLoginAttempt.Purpose.LINK:
        if not link_enabled(provider) or account is None or not account.is_active:
            raise AccountError("Подключить этот мессенджер сейчас нельзя.")
    token, browser_secret = tokens.new_token(), tokens.new_token()
    expires_at = timezone.now() + timedelta(seconds=settings.CUSTOMER_LOGIN_ATTEMPT_SECONDS)
    CustomerLoginAttempt.objects.create(
        purpose=purpose,
        provider=provider,
        token_hash=tokens.digest(token),
        browser_hash=tokens.digest(browser_secret),
        account=account,
        client_hash=tokens.digest(client_key),
        expires_at=expires_at,
    )
    return NewAttempt(token=token, browser_secret=browser_secret, expires_at=expires_at)


# --- Provider confirmation: called by the bots -------------------------------------------

LINK_OPEN_TEXT = (
    "Код для входа в личный кабинет PRO-STOR: {code}\n\n"
    "Введите его на сайте pro-brp.ru в той же вкладке, где начали вход. "
    "Никому не сообщайте этот код: сотрудники PRO-STOR его никогда не спрашивают."
)
LINK_CODE_TEXT = (
    "Код для подключения этого мессенджера к кабинету PRO-STOR: {code}\n\n"
    "Введите его на сайте pro-brp.ru, в кабинете. Никому не сообщайте этот код."
)
ATTEMPT_UNAVAILABLE_TEXT = (
    "Ссылка для входа недействительна или устарела. Начните вход на сайте заново."
)
IDENTITY_TAKEN_TEXT = (
    "Этот мессенджер уже подключён к другому кабинету PRO-STOR. "
    "Подключить его ещё раз нельзя."
)


@dataclass(frozen=True)
class ProviderReply:
    text: str
    code: str = ""


def provider_confirmed(
    *,
    provider: str,
    token: str,
    provider_user_id: int,
    chat_id: int,
    display_name: str = "",
) -> ProviderReply:
    """The messenger's own server says ``provider_user_id`` opened this link.

    Records that identity on the attempt and returns the one-time code to send
    to THAT user. The first user to open a link owns it: a second user opening
    the same link is refused, so a forwarded link cannot be taken over.
    """
    if _verified_user_id(provider_user_id) is None:
        return ProviderReply(ATTEMPT_UNAVAILABLE_TEXT)
    token_hash = tokens.digest(token)
    now = timezone.now()
    with transaction.atomic():
        attempt = (
            CustomerLoginAttempt.objects.select_for_update()
            .filter(token_hash=token_hash, provider=provider)
            .first()
        )
        if (
            attempt is None
            or attempt.expires_at <= now
            or attempt.status not in {attempt.Status.PENDING, attempt.Status.CODE_SENT}
            or attempt.codes_sent >= MAX_CODES_PER_ATTEMPT
        ):
            return ProviderReply(ATTEMPT_UNAVAILABLE_TEXT)
        if attempt.provider_user_id is not None and attempt.provider_user_id != provider_user_id:
            return ProviderReply(ATTEMPT_UNAVAILABLE_TEXT)
        if attempt.purpose == attempt.Purpose.LINK:
            taken = (
                CustomerIdentity.objects.filter(
                    provider=provider, provider_user_id=provider_user_id
                )
                .exclude(account_id=attempt.account_id)
                .exists()
            )
            if taken:
                attempt.status = attempt.Status.FAILED
                attempt.failure = "identity_taken"
                attempt.save(update_fields=["status", "failure"])
                return ProviderReply(IDENTITY_TAKEN_TEXT)
        elif not login_enabled(provider):
            return ProviderReply(ATTEMPT_UNAVAILABLE_TEXT)
        code = tokens.new_code()
        attempt.provider_user_id = provider_user_id
        attempt.provider_chat_id = chat_id
        attempt.display_name = (display_name or "")[:160]
        attempt.code_hash = tokens.code_digest(token_hash, code)
        attempt.codes_sent += 1
        attempt.code_tries = 0
        attempt.status = attempt.Status.CODE_SENT
        attempt.verified_at = attempt.verified_at or now
        attempt.save()
    template = LINK_CODE_TEXT if attempt.purpose == attempt.Purpose.LINK else LINK_OPEN_TEXT
    return ProviderReply(template.format(code=code), code=code)


# --- Completion: the code typed into the same browser ------------------------------------


class Outcome:
    OK = "ok"
    WRONG_CODE = "wrong_code"
    LOCKED = "locked"
    EXPIRED = "expired"
    NOT_READY = "not_ready"
    CONFLICT = "conflict"
    DEACTIVATED = "deactivated"
    INVALID = "invalid"


OUTCOME_TEXT = {
    Outcome.WRONG_CODE: "Код не подошёл. Проверьте сообщение в мессенджере и попробуйте ещё раз.",
    Outcome.LOCKED: "Слишком много неверных кодов. Начните вход заново.",
    Outcome.EXPIRED: "Время на вход истекло. Начните заново.",
    Outcome.NOT_READY: "Сначала откройте мессенджер по ссылке — туда придёт код.",
    Outcome.CONFLICT: "Этот мессенджер уже подключён к другому кабинету.",
    Outcome.DEACTIVATED: "Этот кабинет отключён. Обратитесь в PRO-STOR.",
    Outcome.INVALID: "Вход не начат или уже завершён. Начните заново.",
}


@dataclass(frozen=True)
class Completion:
    outcome: str
    session_token: str = ""
    account_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.outcome == Outcome.OK


def _clean_code(code) -> str:
    code = "".join(str(code or "").split())
    if len(code) != tokens.CODE_DIGITS or not (code.isascii() and code.isdigit()):
        return ""
    return code


def complete_attempt(
    *, browser_secret: str, code: str, session_token: str = ""
) -> Completion:
    """Finish a login (new session) or a link (identity on the signed-in account).

    On PostgreSQL this is the database function; the Python body is the same
    rule set for SQLite and for the internal role.
    """
    code = _clean_code(code)
    if not browser_secret:
        return Completion(Outcome.INVALID)
    new_session_token = tokens.new_token()
    if connection.vendor == "postgresql":
        from .db_security import complete_attempt_in_database

        outcome, account_id = complete_attempt_in_database(
            browser_hash=tokens.digest(browser_secret),
            code=code,
            new_session_hash=tokens.digest(new_session_token),
            current_session_hash=tokens.digest(session_token) if session_token else "",
            session_days=settings.CUSTOMER_SESSION_DAYS,
            max_tries=settings.CUSTOMER_LOGIN_CODE_TRIES,
        )
    else:
        outcome, account_id = _complete_attempt_python(
            browser_hash=tokens.digest(browser_secret),
            code=code,
            new_session_hash=tokens.digest(new_session_token),
            current_session_hash=tokens.digest(session_token) if session_token else "",
        )
    if outcome != Outcome.OK:
        return Completion(outcome)
    return Completion(Outcome.OK, session_token=new_session_token, account_id=account_id)


def _complete_attempt_python(
    *, browser_hash: str, code: str, new_session_hash: str, current_session_hash: str
) -> tuple[str, int | None]:
    now = timezone.now()
    with transaction.atomic():
        attempt = (
            CustomerLoginAttempt.objects.select_for_update()
            .filter(browser_hash=browser_hash)
            .first()
        )
        if attempt is None or attempt.status in {attempt.Status.COMPLETED, attempt.Status.FAILED}:
            return Outcome.INVALID, None
        if attempt.expires_at <= now:
            attempt.status, attempt.failure = attempt.Status.FAILED, "expired"
            attempt.save(update_fields=["status", "failure"])
            return Outcome.EXPIRED, None
        if attempt.status != attempt.Status.CODE_SENT:
            return Outcome.NOT_READY, None
        if attempt.code_tries >= settings.CUSTOMER_LOGIN_CODE_TRIES:
            attempt.status, attempt.failure = attempt.Status.FAILED, "locked"
            attempt.save(update_fields=["status", "failure"])
            return Outcome.LOCKED, None
        if not code or not tokens.same(
            tokens.code_digest(attempt.token_hash, code), attempt.code_hash
        ):
            attempt.code_tries += 1
            locked = attempt.code_tries >= settings.CUSTOMER_LOGIN_CODE_TRIES
            if locked:
                attempt.status, attempt.failure = attempt.Status.FAILED, "locked"
            attempt.save(update_fields=["code_tries", "status", "failure"])
            return (Outcome.LOCKED if locked else Outcome.WRONG_CODE), None

        if attempt.purpose == attempt.Purpose.LINK:
            owner = _session_account_python(current_session_hash)
            if owner is None or owner.pk != attempt.account_id:
                return Outcome.INVALID, None
            try:
                with transaction.atomic():
                    _attach_identity(
                        owner, attempt.provider, attempt.provider_user_id, attempt.display_name
                    )
            except IdentityConflict:
                attempt.status, attempt.failure = attempt.Status.FAILED, "conflict"
                attempt.save(update_fields=["status", "failure"])
                return Outcome.CONFLICT, None
            account = owner
        else:
            try:
                account = _login_account(
                    attempt.provider, attempt.provider_user_id, attempt.display_name
                )
            except AccountDeactivated:
                attempt.status, attempt.failure = attempt.Status.FAILED, "deactivated"
                attempt.save(update_fields=["status", "failure"])
                return Outcome.DEACTIVATED, None
            CustomerSession.objects.create(
                account=account,
                token_hash=new_session_hash,
                expires_at=now + timedelta(days=settings.CUSTOMER_SESSION_DAYS),
            )
            account.last_login_at = now
            account.save(update_fields=["last_login_at", "updated_at"])
            _event(account, CustomerAccountEvent.Kind.LOGIN, provider=attempt.provider)
        attempt.status = attempt.Status.COMPLETED
        attempt.completed_at = now
        attempt.code_hash = ""
        attempt.save(update_fields=["status", "completed_at", "code_hash"])
        _redact_code_messages(attempt.token_hash)
        return Outcome.OK, account.pk


CODE_USED_TEXT = "Код для входа в кабинет PRO-STOR использован."


def _redact_code_messages(token_hash: str) -> None:
    """The code already did its job; do not leave it readable in the outbox."""
    from apps.customer_requests.models import MaxMessage

    MaxMessage.objects.filter(
        dedupe_key__startswith=f"account-code:{token_hash[:32]}:"
    ).update(text=CODE_USED_TEXT)


# --- Identities ---------------------------------------------------------------------------


class IdentityConflict(Exception):
    pass


class AccountDeactivated(Exception):
    pass


def _attach_identity(account, provider, provider_user_id, display_name) -> CustomerIdentity:
    """Give ``account`` this identity, or fail closed if anyone else holds it."""
    existing = CustomerIdentity.objects.filter(
        provider=provider, provider_user_id=provider_user_id
    ).first()
    if existing is not None:
        if existing.account_id != account.pk:
            raise IdentityConflict
        return existing
    if CustomerIdentity.objects.filter(account=account, provider=provider).exists():
        # A second, different MAX (or Telegram) for the same account.
        raise IdentityConflict
    try:
        identity = CustomerIdentity.objects.create(
            account=account,
            provider=provider,
            provider_user_id=provider_user_id,
            display_name=(display_name or "")[:160],
            verified_at=timezone.now(),
        )
    except IntegrityError as exc:
        raise IdentityConflict from exc
    _event(account, CustomerAccountEvent.Kind.IDENTITY_LINKED, provider=provider)
    claim_proven_requests(account, provider, provider_user_id)
    return identity


def _login_account(provider, provider_user_id, display_name) -> CustomerAccount:
    identity = (
        CustomerIdentity.objects.select_related("account")
        .filter(provider=provider, provider_user_id=provider_user_id)
        .first()
    )
    if identity is not None:
        if not identity.account.is_active:
            raise AccountDeactivated
        return identity.account
    return _create_account_with_identity(provider, provider_user_id, display_name)


def _create_account_with_identity(provider, provider_user_id, display_name) -> CustomerAccount:
    """Idempotent under concurrency: the unique identity decides who won."""
    try:
        with transaction.atomic():
            account = CustomerAccount.objects.create(display_name=(display_name or "")[:120])
            _event(account, CustomerAccountEvent.Kind.CREATED, provider=provider)
            _attach_identity(account, provider, provider_user_id, display_name)
            return account
    except IdentityConflict:
        return CustomerIdentity.objects.get(
            provider=provider, provider_user_id=provider_user_id
        ).account


def identity_account(
    provider: str, provider_user_id: int, display_name: str = ""
) -> CustomerAccount | None:
    """The account behind a verified messenger identity at handoff.

    Called by the bots (internal role). Off unless the account feature is
    enabled, so production behaviour does not change before activation.

    * MAX: the account is created on first sight — the zero-friction path, the
      same one a later «Продолжить через MAX» resolves to.
    * Telegram: never creates anything. A Telegram identity reaches an account
      only after a MAX-signed-in customer linked it explicitly.

    A deactivated account is returned as None: it gets no new requests.
    """
    if not account_enabled() or _verified_user_id(provider_user_id) is None:
        return None
    identity = (
        CustomerIdentity.objects.select_related("account")
        .filter(provider=provider, provider_user_id=provider_user_id)
        .first()
    )
    if identity is not None:
        account = identity.account
    elif provider in LOGIN_PROVIDERS:
        account = _create_account_with_identity(provider, provider_user_id, display_name)
    else:
        return None
    return account if account.is_active else None


def ensure_messenger_identity(
    provider: str, provider_user_id: int, display_name: str = ""
) -> CustomerAccount | None:
    """Persist a provider identity for the messenger cabinet.

    This is deliberately independent from ``CUSTOMER_ACCOUNT_ENABLED``:
    messenger identity is the V1 authentication boundary, while the web
    account and its browser sessions remain disabled.  It never links a
    DenisStock Customer by name, phone or username.
    """
    if (
        not settings.CUSTOMER_MESSENGER_CABINET_ENABLED
        or _verified_user_id(provider_user_id) is None
        or provider not in Provider.values
    ):
        return None
    identity = (
        CustomerIdentity.objects.select_related("account")
        .filter(provider=provider, provider_user_id=provider_user_id)
        .first()
    )
    if identity is None:
        account = _create_account_with_identity(provider, provider_user_id, display_name)
    else:
        account = identity.account
    return account if account.is_active else None


def claim_proven_requests(account, provider, provider_user_id) -> int:
    """Attach requests this identity PROVABLY owns, and nothing else.

    Proof is the conversation the messenger's own server bound through a
    one-time link token. Names, usernames and phone text are never evidence,
    and a request already owned by another account is never taken over.
    """
    from apps.customer_requests.models import (
        CustomerRequest,
        MaxConversation,
        TelegramConversation,
    )

    if provider == Provider.MAX:
        owned = MaxConversation.objects.filter(
            customer_user_id=provider_user_id, status=MaxConversation.Status.LINKED
        ).values("request_id")
    elif provider == Provider.TELEGRAM:
        owned = TelegramConversation.objects.filter(
            customer_user_id=provider_user_id, status=TelegramConversation.Status.LINKED
        ).values("request_id")
    else:
        return 0
    claimed = CustomerRequest.objects.filter(
        pk__in=owned, customer_account__isnull=True
    ).update(customer_account=account)
    if claimed:
        _event(
            account,
            CustomerAccountEvent.Kind.REQUESTS_CLAIMED,
            provider=provider,
            count=claimed,
        )
    return claimed


def attach_request_at_handoff(request_obj, provider, provider_user_id, display_name="") -> None:
    """Handoff hook: the verified identity's account owns this request too."""
    account = ensure_messenger_identity(provider, provider_user_id, display_name)
    if account is None:
        return
    from apps.customer_requests.models import CustomerRequest

    CustomerRequest.objects.filter(pk=request_obj.pk, customer_account__isnull=True).update(
        customer_account=account
    )



def unlink_identity(account, provider: str) -> None:
    """Remove a messenger from the account. History stays where it lives.

    Refused for a sign-in identity (MAX in V1): it is the account's only way
    in, and unlinking it would lock the customer out with nothing an employee
    could do about it.
    """
    # No row lock: deleting is idempotent, and the public role is deliberately
    # not granted UPDATE on identities (which SELECT … FOR UPDATE would need).
    with transaction.atomic():
        identity = CustomerIdentity.objects.filter(account=account, provider=provider).first()
        if identity is None:
            return
        if provider in LOGIN_PROVIDERS:
            raise AccountError(
                "MAX — единственный способ входа в кабинет, отключить его нельзя."
            )
        identity.delete()
        _event(account, CustomerAccountEvent.Kind.IDENTITY_UNLINKED, provider=provider)


# --- Sessions -----------------------------------------------------------------------------


def _session_account_python(session_hash: str) -> CustomerAccount | None:
    if not session_hash:
        return None
    now = timezone.now()
    session = (
        CustomerSession.objects.select_related("account")
        .filter(token_hash=session_hash, revoked_at__isnull=True, expires_at__gt=now)
        .first()
    )
    if session is None or not session.account.is_active:
        return None
    return session.account


def session_account(session_token: str) -> CustomerAccount | None:
    if not session_token or len(session_token) > 128:
        return None
    session_hash = tokens.digest(session_token)
    if connection.vendor == "postgresql":
        from .db_security import session_account_id

        account_id = session_account_id(session_hash)
        if account_id is None:
            return None
        return CustomerAccount.objects.filter(pk=account_id).first()
    return _session_account_python(session_hash)


def revoke_session(session_token: str) -> None:
    if not session_token:
        return
    session_hash = tokens.digest(session_token)
    if connection.vendor == "postgresql":
        from .db_security import logout_in_database

        logout_in_database(session_hash)
        return
    with transaction.atomic():
        session = CustomerSession.objects.select_for_update().filter(
            token_hash=session_hash, revoked_at__isnull=True
        ).first()
        if session is None:
            return
        session.revoked_at = timezone.now()
        session.save(update_fields=["revoked_at"])
        _event(session.account, CustomerAccountEvent.Kind.LOGOUT)


# --- Profile, consent, DenisStock link, deactivation --------------------------------------


def update_display_name(account, display_name: str) -> None:
    name = " ".join(str(display_name or "").split())[:120]
    if not name:
        raise AccountError("Укажите имя.")
    if name == account.display_name:
        return
    account.display_name = name
    account.save(update_fields=["display_name", "updated_at"])
    _event(account, CustomerAccountEvent.Kind.PROFILE_CHANGED)


def give_consent(account, purpose: str, *, action: str) -> CustomerConsent:
    version = settings.CUSTOMER_ACCOUNT_CONSENT_VERSION
    if not version:
        raise AccountError("Текст согласия ещё не опубликован.")
    consent = CustomerConsent.objects.create(
        account=account, purpose=purpose, document_version=version, action=action
    )
    _event(account, CustomerAccountEvent.Kind.CONSENT_GIVEN, purpose=purpose, version=version)
    return consent


def withdraw_consent(account, purpose: str) -> int:
    count = CustomerConsent.objects.filter(
        account=account, purpose=purpose, withdrawn_at__isnull=True
    ).update(withdrawn_at=timezone.now())
    if count:
        _event(account, CustomerAccountEvent.Kind.CONSENT_WITHDRAWN, purpose=purpose)
    return count


def has_consent(account, purpose: str) -> bool:
    version = settings.CUSTOMER_ACCOUNT_CONSENT_VERSION
    return bool(version) and CustomerConsent.objects.filter(
        account=account, purpose=purpose, document_version=version, withdrawn_at__isnull=True
    ).exists()


def linked_customer_id(account) -> int | None:
    return (
        CustomerAccountCustomerLink.objects.filter(account=account, unlinked_at__isnull=True)
        .values_list("customer_id", flat=True)
        .first()
    )


def link_customer(account, customer, *, by_user) -> CustomerAccountCustomerLink:
    """An employee states this account is this DenisStock client. Explicit only."""
    with transaction.atomic():
        if CustomerAccountCustomerLink.objects.filter(
            customer=customer, unlinked_at__isnull=True
        ).exclude(account=account).exists():
            raise AccountError("Эта карточка клиента уже привязана к другому кабинету.")
        current = CustomerAccountCustomerLink.objects.select_for_update().filter(
            account=account, unlinked_at__isnull=True
        ).first()
        if current is not None:
            if current.customer_id == customer.pk:
                return current
            raise AccountError("Кабинет уже привязан к другой карточке. Сначала отвяжите её.")
        link = CustomerAccountCustomerLink.objects.create(
            account=account, customer=customer, linked_by=by_user
        )
        _event(
            account,
            CustomerAccountEvent.Kind.CUSTOMER_LINKED,
            actor_user=by_user,
            customer_id=customer.pk,
        )
        return link


def unlink_customer(account, *, by_user) -> None:
    with transaction.atomic():
        link = CustomerAccountCustomerLink.objects.select_for_update().filter(
            account=account, unlinked_at__isnull=True
        ).first()
        if link is None:
            return
        link.unlinked_at, link.unlinked_by = timezone.now(), by_user
        link.save(update_fields=["unlinked_at", "unlinked_by"])
        _event(
            account,
            CustomerAccountEvent.Kind.CUSTOMER_UNLINKED,
            actor_user=by_user,
            customer_id=link.customer_id,
        )


def deactivate_account(account, *, by_user=None) -> None:
    """Close access. Requests and sales are business records and stay intact."""
    with transaction.atomic():
        account = CustomerAccount.objects.select_for_update().get(pk=account.pk)
        if not account.is_active:
            return
        now = timezone.now()
        account.status, account.deactivated_at = CustomerAccount.Status.DEACTIVATED, now
        account.save(update_fields=["status", "deactivated_at", "updated_at"])
        CustomerSession.objects.filter(account=account, revoked_at__isnull=True).update(
            revoked_at=now
        )
        _event(account, CustomerAccountEvent.Kind.DEACTIVATED, actor_user=by_user)
