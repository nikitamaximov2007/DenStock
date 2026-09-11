"""The public hand-off: a customer sends the cart as one request.

This is the only database write the public runtime makes. Everything about
the request itself (validation, price snapshot, availability re-check, the
idempotent insert) belongs to ``apps.customer_requests.services``; this module
only turns the current cart into its input and guards the anonymous edge:

* Only parts that are public right now can be named: the lines come from
  ``build_cart_view``, which maps cart keys through ``public_parts()``.
* A line with nothing available is sent as a supply inquiry; a line asking
  for more than is available blocks sending until the customer fixes it.
* One submission token belongs to one cart content. A browser retry of the
  same form returns the request already created; a changed cart gets a new
  token, so it can never be swallowed by an earlier submission.
* The write runs in one explicit read-write transaction. The public role
  starts every other transaction read-only.
* A small per-address limit and a honeypot field keep casual scripts from
  flooding the operators' queue.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from django.conf import settings
from django.core.cache import cache
from django.db import connection, transaction

from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.policies import current_consent_versions
from apps.customer_requests.services import RequestLineInput, create_customer_request

from .public_cart import CART_SESSION_KEY, LINE_INQUIRY, CartView

SUBMISSION_SESSION_KEY = "public_catalog_request_submission"
HONEYPOT_FIELD = "website"
FORM_FIELDS = ("customer_name", "customer_phone", "preferred_messenger", "comment")


class RequestRefused(ValueError):
    """A customer-facing reason why the request was not sent."""


@dataclass(frozen=True, slots=True)
class Submission:
    """The token of the form on screen and the request it created, if any."""

    token: str
    cart: str
    request: str = ""


def cart_fingerprint(cart: CartView) -> str:
    items = sorted((str(line.card.facts.public_id), line.quantity) for line in cart.lines)
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


def check_rate(request) -> None:
    if cache.get(_client_key(request), 0) >= settings.PUBLIC_REQUEST_RATE_LIMIT:
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
    request.session.pop(CART_SESSION_KEY, None)
    _store(request.session, Submission(submission.token, submission.cart, public_id))
    return public_id, created
