"""The customer's browser: two HttpOnly cookies, nothing else.

* ``prostor_account`` — the session token. Only its digest is in the database.
  A fresh token is issued at every sign-in, so a token planted before login is
  never promoted (no session fixation).
* ``prostor_login`` — ``<browser secret>.<attempt token>`` of the one login or
  link attempt in progress. The secret binds the attempt to this browser: the
  code alone, typed into another browser, completes nothing. The token is kept
  here (HttpOnly) only to redraw the messenger link; the cart cookie is signed
  but not encrypted, so it never holds either.

Neither cookie is the employee session (that is ``sessionid`` on the internal
runtime) and neither shares a signing key with the cart cookie.
"""

from __future__ import annotations

from django.conf import settings

ACCOUNT_COOKIE = "prostor_account"
LOGIN_COOKIE = "prostor_login"
MAX_TOKEN_LENGTH = 128


def _secure() -> bool:
    return bool(getattr(settings, "SESSION_COOKIE_SECURE", False))


def account_token(request) -> str:
    token = request.COOKIES.get(ACCOUNT_COOKIE, "")
    return token if 0 < len(token) <= MAX_TOKEN_LENGTH else ""


def _login_parts(request) -> tuple[str, str]:
    raw = request.COOKIES.get(LOGIN_COOKIE, "")
    secret, _, token = raw.partition(".")
    if not (0 < len(secret) <= MAX_TOKEN_LENGTH and 0 < len(token) <= MAX_TOKEN_LENGTH):
        return "", ""
    return secret, token


def login_secret(request) -> str:
    return _login_parts(request)[0]


def login_token(request) -> str:
    return _login_parts(request)[1]


def looks_signed_in(request) -> bool:
    """A header hint only. It authorizes nothing; every account page re-checks."""
    return bool(account_token(request))


def set_account(response, token: str) -> None:
    response.set_cookie(
        ACCOUNT_COOKIE,
        token,
        max_age=settings.CUSTOMER_SESSION_DAYS * 24 * 60 * 60,
        secure=_secure(),
        httponly=True,
        samesite="Lax",
        path="/",
    )


def clear_account(response) -> None:
    response.delete_cookie(ACCOUNT_COOKIE, path="/", samesite="Lax")


def set_login(response, secret: str, token: str) -> None:
    response.set_cookie(
        LOGIN_COOKIE,
        f"{secret}.{token}",
        max_age=settings.CUSTOMER_LOGIN_ATTEMPT_SECONDS,
        secure=_secure(),
        httponly=True,
        samesite="Lax",
        path="/account/",
    )


def clear_login(response) -> None:
    response.delete_cookie(LOGIN_COOKIE, path="/account/", samesite="Lax")
