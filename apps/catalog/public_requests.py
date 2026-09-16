"""The public hand-off: a customer sends the cart as one request.

This is the only database write the public runtime makes. Everything about
the request itself (validation, price snapshot, availability re-check, the
idempotent insert) belongs to ``apps.customer_requests.services``; this module
only turns the current cart into its input and guards the anonymous edge:

* Only parts that are public right now can be named: the lines come from
  ``build_cart_view``, which maps cart keys through ``public_parts()``.
* A line with nothing available is sent as a supply inquiry; a line asking
  for more than is available blocks sending until the customer fixes it.
* One submission token belongs to one cart content, line states included. A
  browser retry of the same form returns the request already created; a
  changed cart (or a line that ran out of stock meanwhile) gets a new token
  and is confirmed again, so it can never be swallowed by an earlier
  submission or change meaning unseen.
* The write runs in one explicit read-write transaction. The public role
  starts every other transaction read-only.
* A small per-address limit and a honeypot field keep casual scripts from
  flooding the operators' queue.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction

from apps.customer_requests.messengers import (
    MessengerLinkError,
    issue_initial_messenger_link,
    messenger_start_url,
    telegram_start_url,  # noqa: F401 - the success view reads it from here
)
from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.policies import current_consent_versions
from apps.customer_requests.services import (
    RequestLineInput,
    create_customer_request,
    submission_key_hash,
)
from apps.customer_requests.telegram_service import request_insert_proof

from .public_cart import CART_SESSION_KEY, LINE_INQUIRY, CartView

SUBMISSION_SESSION_KEY = "public_catalog_request_submission"
# Messenger handoff state of the request this browser just sent: which
# messenger it chose and how many links it asked for. Never a token. The key
# keeps its original Telegram name so sessions issued before MAX still work;
# the ``messenger`` inside says which channel it is for.
TELEGRAM_SESSION_KEY = "public_catalog_request_telegram"
MESSENGER_SESSION_KEY = TELEGRAM_SESSION_KEY
HONEYPOT_FIELD = "website"
FORM_FIELDS = ("customer_name", "customer_phone", "preferred_messenger", "comment")
# A browser can fail to leave the page (an extension, a blocked handoff, a
# closed Telegram or MAX). The customer may ask for a fresh link a few times;
# the database caps it too, so a replayed cookie cannot mint links without end.
MAX_TELEGRAM_LINK_ATTEMPTS = 3
MAX_MESSENGER_LINK_ATTEMPTS = MAX_TELEGRAM_LINK_ATTEMPTS
logger = logging.getLogger(__name__)


class RequestRefused(ValueError):
    """A customer-facing reason why the request was not sent."""


@dataclass(frozen=True, slots=True)
class Submission:
    """The token of the form on screen and the request it created, if any."""

    token: str
    cart: str
    request: str = ""


def cart_fingerprint(cart: CartView) -> str:
    """What the customer confirmed: parts, quantities and each line's state.

    The state is part of it so that a line that ran out of stock (or came
    back) after the form was shown is confirmed again, instead of being sent
    silently as a supply inquiry (or as a normal line).
    """
    items = sorted(
        (str(line.card.facts.public_id), line.quantity, line.state) for line in cart.lines
    )
    return hashlib.sha256(repr(items).encode()).hexdigest()


def stored_submission(session) -> Submission | None:
    raw = session.get(SUBMISSION_SESSION_KEY)
    if not isinstance(raw, dict):
        return None
    token, cart, request = raw.get("token"), raw.get("cart"), raw.get("request", "")
    if not all(isinstance(value, str) for value in (token, cart, request)) or not token:
        return None
    return Submission(token=token, cart=cart, request=request)


def _store(session, submission: Submission) -> None:
    session[SUBMISSION_SESSION_KEY] = {
        "token": submission.token,
        "cart": submission.cart,
        "request": submission.request,
    }


def form_token(session, cart: CartView) -> str:
    """The token for this cart content; a new one once a request was sent."""
    fingerprint = cart_fingerprint(cart)
    stored = stored_submission(session)
    if stored and stored.cart == fingerprint and not stored.request:
        return stored.token
    token = secrets.token_urlsafe(32)
    _store(session, Submission(token=token, cart=fingerprint))
    return token


def matching_submission(session, submitted_token: str) -> Submission | None:
    stored = stored_submission(session)
    if stored and hmac.compare_digest(stored.token, str(submitted_token or "")):
        return stored
    return None


def request_reference(public_id) -> str:
    """The short number a customer can read out on the phone."""
    return CustomerRequest.reference_for(public_id)


# --- Abuse limits ----------------------------------------------------------------------
#
# Per process and per client address. catalog-web is reachable only through
# Caddy, which replaces any client-sent X-Forwarded-For with the address it
# saw, so the right-most entry is the client. The limit is deliberately
# coarse: it stops a script from filling the queue, not a determined botnet.


def _client_key(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    address = forwarded.rsplit(",", 1)[-1].strip() if forwarded else ""
    address = address or request.META.get("REMOTE_ADDR", "")
    return "public-request:" + hashlib.sha256(address.encode()).hexdigest()[:32]


def check_rate(request, submission: Submission) -> None:
    """Refuse a NEW request over the limit; a retry of a sent one always passes.

    Two clicks can arrive together, before either response has recorded the
    request in the cookie. The one that loses the race must still land on the
    request that was created, not on "too many requests".
    """
    if cache.get(_client_key(request), 0) < settings.PUBLIC_REQUEST_RATE_LIMIT:
        return
    already_sent = CustomerRequest.objects.filter(
        submission_key_hash=submission_key_hash(submission.token)
    ).exists()
    if not already_sent:
        raise RequestRefused("Слишком много заявок подряд. Попробуйте через несколько минут.")


def _count(request) -> None:
    key = _client_key(request)
    if not cache.add(key, 1, settings.PUBLIC_REQUEST_RATE_WINDOW_SECONDS):
        try:
            cache.incr(key)
        except ValueError:
            cache.set(key, 1, settings.PUBLIC_REQUEST_RATE_WINDOW_SECONDS)


def looks_automated(post) -> bool:
    """The honeypot field is hidden from people; only form-filling scripts fill it."""
    return bool(str(post.get(HONEYPOT_FIELD) or "").strip())


# --- The write ---------------------------------------------------------------------------


def line_inputs(cart: CartView) -> list[RequestLineInput]:
    return [
        RequestLineInput(
            part_id=line.part_id,
            quantity=line.quantity,
            supply_inquiry=line.state == LINE_INQUIRY,
        )
        for line in cart.lines
    ]


def _read_write_transaction() -> None:
    """Open the current transaction for writing.

    The public role's sessions default to read-only. This must be the first
    statement of the transaction; on a read-write session it is a no-op.
    """
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ WRITE")


def send_cart(request, cart: CartView, submission: Submission, values) -> tuple[str, bool]:
    """Create the request for this cart; returns its public id and whether it is new.

    Raises ``CustomerRequestError`` for a customer-facing validation failure and
    ``apps.operations.write_guard.BusinessWriteBlocked`` while writes are frozen.
    """
    privacy_policy_version, consent_version = current_consent_versions()
    with transaction.atomic():
        _read_write_transaction()
        customer_request, created = create_customer_request(
            customer_name=values.get("customer_name", ""),
            customer_phone=values.get("customer_phone", ""),
            preferred_messenger=values.get("preferred_messenger", ""),
            comment=values.get("comment", ""),
            lines=line_inputs(cart),
            privacy_policy_version=privacy_policy_version,
            personal_data_consent_version=consent_version,
            submission_key=submission.token,
        )
    public_id = str(customer_request.public_id)
    if created:
        _count(request)
        request.session[TELEGRAM_SESSION_KEY] = {
            "request": public_id,
            "messenger": customer_request.preferred_messenger,
            # A signed-cookie session authenticates its contents but does not
            # encrypt them.  Keep only non-secret state here: the raw Telegram
            # token is generated after the customer clicks the local POST form.
            "link_attempts": 0,
            "link_unavailable": False,
        }
    request.session.pop(CART_SESSION_KEY, None)
    _store(request.session, Submission(submission.token, submission.cart, public_id))
    return public_id, created


def _link_attempts(stored: dict) -> int:
    """Attempts made by this browser, tolerating a session from the old flow."""
    attempts = stored.get("link_attempts")
    if isinstance(attempts, int) and attempts >= 0:
        return attempts
    return 1 if stored.get("link_issued") else 0


def messenger_success(session, public_id, channel: str) -> dict:
    """What the success page says about one messenger, from this browser's cookie only.

    Keys are prefixed with the channel (``telegram_*``, ``max_*``).
    """
    prefix = f"{channel}_"
    stored = session.get(MESSENGER_SESSION_KEY)
    if (
        not isinstance(stored, dict)
        or stored.get("request") != str(public_id)
        or stored.get("messenger") != channel
    ):
        return {f"{prefix}selected": False, f"{prefix}ready": False}
    try:
        ready = messenger_start_url(channel, "a" * 43) is not None
    except MessengerLinkError:
        ready = False
    ready = ready and not stored.get("link_unavailable", False)
    attempts = _link_attempts(stored)
    attempts_left = max(MAX_MESSENGER_LINK_ATTEMPTS - attempts, 0)
    return {
        f"{prefix}selected": True,
        f"{prefix}ready": ready,
        # The handoff is a redirect to another origin: the browser may refuse
        # it or the customer may come back. Keep offering it until the cap.
        f"{prefix}can_continue": ready and attempts_left > 0,
        f"{prefix}retry": ready and attempts_left > 0 and attempts > 0,
        f"{prefix}attempts_left": attempts_left,
    }


def telegram_success(session, public_id) -> dict:
    """What the success page says about Telegram, from this browser's cookie only."""
    return messenger_success(session, public_id, CustomerRequest.Messenger.TELEGRAM)


def max_success(session, public_id) -> dict:
    """What the success page says about MAX, from this browser's cookie only."""
    return messenger_success(session, public_id, CustomerRequest.Messenger.MAX)


def issue_success_telegram_link(session, public_id) -> str:
    return issue_success_link(session, public_id, CustomerRequest.Messenger.TELEGRAM)


def issue_success_max_link(session, public_id) -> str:
    return issue_success_link(session, public_id, CustomerRequest.Messenger.MAX)


def issue_success_link(session, public_id, channel: str) -> str:
    """Create a deep link only after the customer's local POST.

    The raw token exists only long enough to form the redirect to Telegram. It
    is never put in the HTML page or the signed (but readable) session cookie.
    A handoff the browser did not complete may be retried up to
    ``MAX_TELEGRAM_LINK_ATTEMPTS`` times; the database enforces the same cap,
    and consuming one link revokes the request's other unused ones.
    """
    stored = session.get(MESSENGER_SESSION_KEY)
    submission = stored_submission(session)
    if (
        not isinstance(stored, dict)
        or stored.get("request") != str(public_id)
        or stored.get("messenger") != channel
        or _link_attempts(stored) >= MAX_MESSENGER_LINK_ATTEMPTS
        or submission is None
        or submission.request != str(public_id)
    ):
        return ""
    try:
        with transaction.atomic():
            _read_write_transaction()
            # The public role may read only this harmless identity pair.
            customer_request = CustomerRequest.objects.only("pk", "public_id").get(
                public_id=public_id
            )
            # This remains optional to the request.  A messenger-only database
            # fault must be contained by this savepoint.
            with transaction.atomic(), request_insert_proof(submission.token):
                token = issue_initial_messenger_link(customer_request, channel)
    except Exception as exc:  # noqa: BLE001 - do not leak optional faults to the customer
        logger.warning(
            "%s link setup unavailable for request %s: %s",
            channel, public_id, type(exc).__name__,
        )
        stored["link_unavailable"] = True
        session[MESSENGER_SESSION_KEY] = stored
        return ""
    if not token:
        stored["link_unavailable"] = True
        session[MESSENGER_SESSION_KEY] = stored
        return ""
    stored["link_attempts"] = _link_attempts(stored) + 1
    session[MESSENGER_SESSION_KEY] = stored
    return token
