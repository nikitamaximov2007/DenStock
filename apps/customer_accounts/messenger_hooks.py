"""Where the MAX and Telegram bots hand account events to the account domain.

The bots run with the internal role and receive each user's identity from the
messenger's own server — the only place an identity is ever trusted. Two
events matter:

* a start payload ``acc_<token>`` — the user opened a login (MAX) or link
  (Telegram) link from the website: record the identity, send the one-time
  code to that same user;
* a request handoff — the user opened a request's own deep link: the request
  joins that identity's account (MAX creates it on first sight; Telegram only
  joins an account a MAX-signed-in customer already linked).
"""

from __future__ import annotations

from . import services, tokens
from .models import Provider

CODE_KEY_PREFIX = "account-code:"


def display_name(user) -> str:
    if not isinstance(user, dict):
        return ""
    parts = [str(user.get("first_name") or "").strip(), str(user.get("last_name") or "").strip()]
    name = " ".join(part for part in parts if part)
    return (name or str(user.get("name") or "").strip())[:160]


def code_message_key(token: str, event_key: str) -> str:
    """Per event, so a redelivered webhook never rotates the code twice."""
    return f"{CODE_KEY_PREFIX}{tokens.digest(token)[:32]}:{event_key}"[:160]


def max_start(*, payload: str, user_id: int, chat_id: int, user, event_key: str) -> bool:
    """Handle an account payload in MAX. False means: not ours, carry on."""
    token = tokens.token_from_payload(payload)
    if token is None:
        return False
    from apps.customer_requests import max_service
    from apps.customer_requests.models import MaxMessage

    key = code_message_key(token, event_key)
    if MaxMessage.objects.filter(dedupe_key=key).exists():
        return True  # the same webhook event again: the code already went out
    reply = services.provider_confirmed(
        provider=Provider.MAX,
        token=token,
        provider_user_id=user_id,
        chat_id=chat_id,
        display_name=display_name(user),
    )
    max_service.queue_message(chat_id=chat_id, text=reply.text, dedupe_key=key)
    return True


def telegram_start(*, argument: str, user_id: int, chat_id: int, user) -> str | None:
    """Handle an account payload in Telegram. None means: not ours, carry on.

    Telegram updates are consumed exactly once (the offset commits with the
    update), so the reply goes straight back instead of through a stored row.
    """
    token = tokens.token_from_payload(argument)
    if token is None:
        return None
    return services.provider_confirmed(
        provider=Provider.TELEGRAM,
        token=token,
        provider_user_id=user_id,
        chat_id=chat_id,
        display_name=display_name(user),
    ).text


def max_handoff(request_obj, *, user_id: int, name: str = "") -> None:
    """A request's MAX deep link was opened by this verified MAX user."""
    account = services.ensure_messenger_identity(
        Provider.MAX, user_id, name or request_obj.customer_name
    )
    if account is not None:
        request_obj.__class__.objects.filter(
            pk=request_obj.pk, customer_account__isnull=True
        ).update(customer_account=account)


def telegram_handoff(request_obj, *, user_id) -> None:
    """Persist the verified Telegram identity without enabling web login."""
    account = services.ensure_messenger_identity(Provider.TELEGRAM, user_id)
    if account is not None:
        request_obj.__class__.objects.filter(
            pk=request_obj.pk, customer_account__isnull=True
        ).update(customer_account=account)
