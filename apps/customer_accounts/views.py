"""The customer account pages on the public runtime (pro-brp.ru/account/…).

Every page answers 404 until ``CUSTOMER_ACCOUNT_ENABLED``. Every signed-in page
runs inside one transaction bound to the browser's session
(``db_security.account_transaction``): on PostgreSQL that binding is what the
database checks, so a page can only ever read the data of the session that
asked for it. Ownership never comes from a URL: request pages look up by the
opaque ``public_id`` among the account's OWN requests, purchase pages among the
account's OWN completed sales, and anything else is simply "not found".
"""

from __future__ import annotations

import hashlib
from functools import wraps
from urllib.parse import quote

from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from apps.catalog.search import MAX_QUERY_LENGTH

from . import history, reorder, services, tokens, web_session
from .db_security import account_transaction
from .models import CustomerConsent, CustomerIdentity, CustomerLoginAttempt, Provider

LOGIN_URL_NAME = "customer_account_login"


def _require_enabled() -> None:
    if not services.account_enabled():
        raise Http404


def _render(request, template, context=None, *, status=200):
    from apps.catalog.public_cart import cart_size

    base = {
        "cart_lines": cart_size(request.session),
        "indexing": False,
        "account_enabled": services.account_enabled(),
        "account_signed_in": web_session.looks_signed_in(request),
        "max_query_length": MAX_QUERY_LENGTH,
    }
    base.update(context or {})
    return render(request, template, base, status=status)


def _client_key(request) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    address = forwarded.rsplit(",", 1)[-1].strip() if forwarded else ""
    address = address or request.META.get("REMOTE_ADDR", "")
    return "customer-login:" + hashlib.sha256(address.encode()).hexdigest()[:32]


def _over_rate(request) -> bool:
    """Coarse, per client address: stops a script from flooding attempts."""
    key = _client_key(request)
    window = settings.CUSTOMER_LOGIN_RATE_WINDOW_SECONDS
    if not cache.add(key, 1, window):
        try:
            count = cache.incr(key)
        except ValueError:
            cache.set(key, 1, window)
            count = 1
        return count > settings.CUSTOMER_LOGIN_RATE_LIMIT
    return False


def signed_in(write: bool = False):
    """Run the view for the live session's account, or send the browser to log in."""

    def decorate(view):
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            _require_enabled()
            token = web_session.account_token(request)
            if not token:
                return redirect(LOGIN_URL_NAME)
            with account_transaction(tokens.digest(token), write=write):
                account = services.session_account(token)
                if account is None:
                    response = redirect(LOGIN_URL_NAME)
                    web_session.clear_account(response)
                    return response
                return view(request, account, *args, **kwargs)

        return wrapper

    return decorate


# --- Sign in -------------------------------------------------------------------------------


@require_safe
def login(request):
    _require_enabled()
    return _render(
        request,
        "customer_accounts/login.html",
        {"max_enabled": services.login_enabled(Provider.MAX)},
    )


def _max_link(payload: str) -> str:
    username = settings.MAX_BOT_USERNAME
    base = settings.MAX_DEEP_LINK_BASE_URL.rstrip("/")
    return f"{base}/{quote(username)}?start={quote(payload)}" if username else ""


def _telegram_link(payload: str) -> str:
    username = settings.TELEGRAM_BOT_USERNAME
    return f"https://t.me/{quote(username)}?start={quote(payload)}" if username else ""


@require_POST
def login_max(request):
    """Open a one-time MAX login attempt bound to this browser."""
    _require_enabled()
    if not services.login_enabled(Provider.MAX) or not settings.MAX_BOT_USERNAME:
        messages.error(request, "Вход через MAX сейчас недоступен.")
        return redirect(LOGIN_URL_NAME)
    if _over_rate(request):
        messages.error(request, "Слишком много попыток входа. Попробуйте через несколько минут.")
        return redirect(LOGIN_URL_NAME)
    with account_transaction("", write=True):
        attempt = services.create_attempt(
            purpose=CustomerLoginAttempt.Purpose.LOGIN,
            provider=Provider.MAX,
            client_key=_client_key(request),
        )
    response = redirect("customer_account_login_code")
    web_session.set_login(response, attempt.browser_secret, attempt.token)
    return response


@require_http_methods(["GET", "HEAD", "POST"])
def login_code(request):
    """Open MAX, then type the code MAX sent into this same browser."""
    _require_enabled()
    secret = web_session.login_secret(request)
    if not secret:
        messages.error(request, "Вход не начат или устарел. Начните заново.")
        return redirect(LOGIN_URL_NAME)
    consent_required = bool(settings.CUSTOMER_ACCOUNT_CONSENT_VERSION)
    if request.method == "POST":
        if consent_required and request.POST.get("consent_account") != "1":
            messages.error(
                request, "Без согласия на обработку персональных данных кабинет не открыть."
            )
            return redirect("customer_account_login_code")
        completion = services.complete_attempt(
            browser_secret=secret, code=request.POST.get("code", "")
        )
        if not completion.ok:
            messages.error(
                request, services.OUTCOME_TEXT.get(completion.outcome, "Не удалось войти.")
            )
            if completion.outcome in {"locked", "expired", "invalid", "deactivated"}:
                response = redirect(LOGIN_URL_NAME)
                web_session.clear_login(response)
                return response
            return redirect("customer_account_login_code")
        response = redirect("customer_account_home")
        web_session.clear_login(response)
        web_session.set_account(response, completion.session_token)
        if consent_required:
            with account_transaction(tokens.digest(completion.session_token), write=True):
                account = services.session_account(completion.session_token)
                if account is not None and not services.has_consent(
                    account, CustomerConsent.Purpose.ACCOUNT
                ):
                    services.give_consent(
                        account, CustomerConsent.Purpose.ACCOUNT, action="checkbox_login_code_form"
                    )
        return response
    return _render(
        request,
        "customer_accounts/login_code.html",
        {
            "deep_link": _max_link(tokens.start_payload(web_session.login_token(request))),
            "consent_required": consent_required,
            "consent_version": settings.CUSTOMER_ACCOUNT_CONSENT_VERSION,
        },
    )


@require_POST
def logout(request):
    _require_enabled()
    token = web_session.account_token(request)
    if token:
        services.revoke_session(token)
    response = redirect("public_catalog_root")
    web_session.clear_account(response)
    web_session.clear_login(response)
    return response


# --- Account pages ------------------------------------------------------------------------


def _identities(account) -> dict[str, CustomerIdentity]:
    identities = CustomerIdentity.objects.filter(account=account)
    return {identity.provider: identity for identity in identities}


@require_safe
@signed_in()
def home(request, account):
    requests = history.account_requests(account)
    purchases = history.account_purchases(account)
    identities = _identities(account)
    return _render(
        request,
        "customer_accounts/home.html",
        {
            "account": account,
            "active_requests": [r for r in requests if r.is_active],
            "recent_request": requests[0] if requests else None,
            "recent_purchase": purchases[0] if purchases else None,
            "max_identity": identities.get(Provider.MAX),
            "telegram_identity": identities.get(Provider.TELEGRAM),
        },
    )


@require_safe
@signed_in()
def requests_list(request, account):
    return _render(
        request,
        "customer_accounts/requests.html",
        {"account": account, "requests": history.account_requests(account)},
    )


@require_safe
@signed_in()
def request_detail(request, account, public_id):
    summary = history.account_request(account, public_id)
    if summary is None:
        raise Http404
    return _render(
        request, "customer_accounts/request_detail.html", {"account": account, "req": summary}
    )


@require_safe
@signed_in()
def purchases_list(request, account):
    return _render(
        request,
        "customer_accounts/purchases.html",
        {"account": account, "purchases": history.account_purchases(account)},
    )


@require_safe
@signed_in()
def purchase_detail(request, account, number):
    purchase = history.account_purchase(account, number)
    if purchase is None:
        raise Http404
    return _render(
        request,
        "customer_accounts/purchase_detail.html",
        {"account": account, "purchase": purchase},
    )


@require_http_methods(["GET", "HEAD", "POST"])
@signed_in()
def purchase_reorder(request, account, number):
    """Preview on GET; «Добавить в корзину» on POST writes only the cart cookie."""
    purchase = history.account_purchase(account, number)
    if purchase is None:
        raise Http404
    lines = reorder.preview(purchase)
    if request.method == "POST":
        added = reorder.add_to_cart(request.session, lines)
        if added:
            messages.success(request, "Позиции добавлены в корзину. Проверьте количество и цены.")
        else:
            messages.error(request, "Ни одну позицию из этой покупки сейчас добавить нельзя.")
        return redirect("public_catalog_cart")
    return _render(
        request,
        "customer_accounts/reorder.html",
        {
            "account": account,
            "purchase": purchase,
            "lines": lines,
            "usable": any(line.usable for line in lines),
        },
    )


# --- Messengers -----------------------------------------------------------------------------


@require_safe
@signed_in()
def messengers(request, account):
    identities = _identities(account)
    return _render(
        request,
        "customer_accounts/messengers.html",
        {
            "account": account,
            "max_identity": identities.get(Provider.MAX),
            "telegram_identity": identities.get(Provider.TELEGRAM),
            "telegram_linkable": services.link_enabled(Provider.TELEGRAM)
            and bool(settings.TELEGRAM_BOT_USERNAME),
        },
    )


@require_POST
@signed_in(write=True)
def telegram_link_start(request, account):
    if not services.link_enabled(Provider.TELEGRAM) or not settings.TELEGRAM_BOT_USERNAME:
        messages.error(request, "Подключить Telegram сейчас нельзя.")
        return redirect("customer_account_messengers")
    if _over_rate(request):
        messages.error(request, "Слишком много попыток. Попробуйте через несколько минут.")
        return redirect("customer_account_messengers")
    attempt = services.create_attempt(
        purpose=CustomerLoginAttempt.Purpose.LINK,
        provider=Provider.TELEGRAM,
        client_key=_client_key(request),
        account=account,
    )
    response = redirect("customer_account_telegram_code")
    web_session.set_login(response, attempt.browser_secret, attempt.token)
    return response


@require_http_methods(["GET", "HEAD", "POST"])
def telegram_link_code(request):
    _require_enabled()
    token = web_session.account_token(request)
    secret = web_session.login_secret(request)
    if not token:
        return redirect(LOGIN_URL_NAME)
    if not secret:
        messages.error(request, "Подключение не начато или устарело. Начните заново.")
        return redirect("customer_account_messengers")
    if request.method == "POST":
        completion = services.complete_attempt(
            browser_secret=secret, code=request.POST.get("code", ""), session_token=token
        )
        if not completion.ok:
            messages.error(
                request, services.OUTCOME_TEXT.get(completion.outcome, "Не удалось подключить.")
            )
            if completion.outcome == "wrong_code":
                return redirect("customer_account_telegram_code")
            response = redirect("customer_account_messengers")
            web_session.clear_login(response)
            return response
        messages.success(request, "Telegram подключён к кабинету.")
        response = redirect("customer_account_messengers")
        web_session.clear_login(response)
        return response
    return _render(
        request,
        "customer_accounts/telegram_code.html",
        {"deep_link": _telegram_link(tokens.start_payload(web_session.login_token(request)))},
    )


@require_POST
@signed_in(write=True)
def telegram_unlink(request, account):
    try:
        services.unlink_identity(account, Provider.TELEGRAM)
    except services.AccountError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Telegram отключён от кабинета. Переписка по заявкам сохранена.")
    return redirect("customer_account_messengers")


# --- Profile --------------------------------------------------------------------------------


@require_http_methods(["GET", "HEAD", "POST"])
def profile(request):
    _require_enabled()
    if request.method == "POST":
        return _profile_save(request)
    return _profile_show(request)


@signed_in()
def _profile_show(request, account):
    identities = _identities(account)
    return _render(
        request,
        "customer_accounts/profile.html",
        {
            "account": account,
            "max_identity": identities.get(Provider.MAX),
            "telegram_identity": identities.get(Provider.TELEGRAM),
            "consent_given": services.has_consent(account, CustomerConsent.Purpose.ACCOUNT),
            "consent_version": settings.CUSTOMER_ACCOUNT_CONSENT_VERSION,
        },
    )


@signed_in(write=True)
def _profile_save(request, account):
    try:
        services.update_display_name(account, request.POST.get("display_name", ""))
    except services.AccountError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, "Имя сохранено.")
    return redirect(reverse("customer_account_profile"))
