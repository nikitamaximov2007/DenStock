"""Secrets of the customer account: generated here, stored only as digests."""

from __future__ import annotations

import hashlib
import hmac
import secrets

# The bot start payload is `<prefix><token>`. MAX allows [A-Za-z0-9_-] up to
# 512 chars, Telegram the same set up to 64; a 32-byte urlsafe token is 43.
LOGIN_PREFIX = "acc_"
TOKEN_BYTES = 32
CODE_DIGITS = 6


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def new_code() -> str:
    """Six uniformly random digits (never ``random``)."""
    return f"{secrets.randbelow(10 ** CODE_DIGITS):0{CODE_DIGITS}d}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def code_digest(attempt_token_hash: str, code: str) -> str:
    """Bound to its attempt: the same six digits mean nothing elsewhere."""
    return digest(f"{attempt_token_hash}:{code}")


def same(a: str, b: str) -> bool:
    return bool(a) and bool(b) and hmac.compare_digest(a, b)


def start_payload(token: str) -> str:
    return f"{LOGIN_PREFIX}{token}"


def token_from_payload(payload) -> str | None:
    """The attempt token inside a bot start payload, or None if it is not ours."""
    if not isinstance(payload, str):
        return None
    payload = payload.strip()
    if not payload.startswith(LOGIN_PREFIX):
        return None
    token = payload[len(LOGIN_PREFIX):]
    if not 20 <= len(token) <= 64:
        return None
    if not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in token):
        return None
    return token
