"""Customer-facing PRO-STOR catalog views, served only by the public runtime.

Views orchestrate. Reads live in ``public_catalog``, ``public_photos`` and the
Stage 1 facades; the cart lives in ``public_cart``; sending the cart as a
customer request lives in ``public_requests``. The public process has no
authentication and no internal route. Its database role reads the catalog and
may only insert a new customer request: that is the one business write here,
and it never reserves, sells or moves stock.
"""

from uuid import UUID

from django.contrib import messages
from django.db import connection
from django.http import Http404, HttpResponse, HttpResponseNotModified, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.cache import patch_cache_control
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_safe

from apps.customer_requests.services import CustomerRequestError
from apps.operations.write_guard import BusinessWriteBlocked

from . import public_requests, public_seo
from .public_cart import (
    LINE_INQUIRY,
    LINE_SHORT,
    MAX_CART_QUANTITY,
    CartError,
    build_cart_view,
    cart_size,
    quantity_in_cart,
    read_cart,
    remove_line,
    set_line,
)
from .public_catalog import (
    APPLICATION_LABELS,
    cards_by_id,
    part_relations,
    public_parts,
    search_catalog,
)
from .public_photos import part_photos, rendition_for
from .search import MAX_QUERY_LENGTH

PHOTO_MAX_AGE = 24 * 60 * 60
CRAWLER_FILE_MAX_AGE = 60 * 60


def _render(request, template, context=None, *, status=200):
    """Every public page shares the header state and the indexing policy."""
    base = {
        "cart_lines": cart_size(request.session),
        "indexing": public_seo.indexing_enabled(),
        "max_query_length": MAX_QUERY_LENGTH,
    }
    base.update(context or {})
    return render(request, template, base, status=status)


@require_safe
def public_root(request):
    return _render(
        request,
        "public_catalog/home.html",
        {"canonical_url": public_seo.absolute_url(request, reverse("public_catalog_root"))},
    )


@require_safe
def public_search(request):
    result = search_catalog(request.GET.get("q"), request.GET)
    return _render(
        request,
        "public_catalog/search.html",
        {
            "result": result,
            "query": result.query,
            "application_labels": APPLICATION_LABELS,
            "previous_url": result.url(page=result.page - 1) if result.has_previous else "",
            "next_url": result.url(page=result.page + 1) if result.has_next else "",
            "reset_url": result.url(reset=True),
            "chips": _filter_chips(result),
            "in_cart": {UUID(key) for key in read_cart(request.session)},
        },
    )


def _filter_chips(result):
    """Selected filters as removable chips; every link is a complete URL."""
    filters = result.filters
    chips = []
    if filters.in_stock:
        chips.append(("Только в наличии", result.url(drop="in_stock")))
    if filters.application:
        chips.append((APPLICATION_LABELS[filters.application], result.url(drop="application")))
    if filters.manufacturer:
        chips.append((filters.manufacturer, result.url(drop="manufacturer")))
    if filters.relation:
        label = next(o.label for o in result.facets.relations if o.value == filters.relation)
        chips.append((label, result.url(drop="relation")))
    return chips


@require_safe
def public_part_detail(request, public_id):
    part_id = public_parts().filter(public_id=public_id).values_list("pk", flat=True).first()
    if part_id is None:
        raise Http404
    card = cards_by_id([part_id])[part_id]
    canonical_url = public_seo.absolute_url(
        request, reverse("public_catalog_part", args=[public_id])
    )
    photos = part_photos(part_id)
    photo_urls = [
        public_seo.absolute_url(request, _photo_path(photo, "detail")) for photo in photos
    ]
    return _render(
        request,
        "public_catalog/part_detail.html",
        {
            "card": card,
            "facts": card.facts,
            "photos": photos,
            "relations": part_relations(part_id),
            "canonical_url": canonical_url,
            "page_title": public_seo.part_title(card),
            "meta_description": public_seo.part_description(card),
            "og_image": photo_urls[0] if photo_urls else "",
            "json_ld": public_seo.json_for_script(
                public_seo.product_json_ld(card, url=canonical_url, images=photo_urls)
            ),
            "in_cart": quantity_in_cart(request.session, public_id),
            "quantity_max": _quantity_max(card),
        },
    )


def _quantity_max(card):
    """Browser-side hint only; the server re-checks availability on submit."""
    if not card.in_stock:
        return MAX_CART_QUANTITY
    return max(1, min(int(card.facts.available_quantity), MAX_CART_QUANTITY))


def _photo_path(photo, variant):
    path = reverse("public_catalog_photo", args=[photo.public_id, variant])
    return f"{path}?v={photo.version}" if photo.version else path


@require_safe
def public_photo(request, public_id, variant):
    """One published rendition, re-checked against publication on every request."""
    rendition = rendition_for(public_id, variant)
    if rendition is None:
        raise Http404
    etag = f'"{rendition["sha256"]}"'
    if request.headers.get("If-None-Match") == etag:
        response = HttpResponseNotModified()
    else:
        response = HttpResponse(bytes(rendition["data"]), content_type=rendition["content_type"])
        response["Content-Length"] = str(rendition["byte_size"])
    response["ETag"] = etag
    patch_cache_control(response, public=True, max_age=PHOTO_MAX_AGE)
    return response


@require_safe
@never_cache
def public_cart(request):
    cart = build_cart_view(request.session)
    if cart.removed_lines:
        messages.info(
            request,
            "Некоторых деталей больше нет в каталоге. Мы убрали их из корзины.",
        )
    return _render(
        request,
        "public_catalog/cart.html",
        {
            "cart": cart,
            "line_inquiry": LINE_INQUIRY,
            "line_short": LINE_SHORT,
            "max_cart_quantity": MAX_CART_QUANTITY,
        },
    )


def _safe_next(request):
    """Where to go after a cart change: the cart, or a local catalog page only."""
    value = str(request.POST.get("next") or "")
    if value == "cart":
        return reverse("public_catalog_cart")
    local = value.startswith(("/search/", "/parts/")) and url_has_allowed_host_and_scheme(
        value, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    )
    return value if local else ""


@require_POST
@never_cache
def public_cart_add(request, public_id):
    """Set a line's quantity. Any client price or extra field is ignored.

    ``if_absent=1`` (the button on a result card) adds one unit only when the
    part is not in the cart yet, so a second click never silently overwrites a
    quantity the customer chose on the part page or in the cart.
    """
    part_id = public_parts().filter(public_id=public_id).values_list("pk", flat=True).first()
    if part_id is None:
        raise Http404
    card = cards_by_id([part_id])[part_id]
    next_url = _safe_next(request)
    if request.POST.get("if_absent") == "1" and quantity_in_cart(request.session, public_id):
        messages.info(request, f"{card.display_name}: уже в корзине.")
        return redirect(next_url or reverse("public_catalog_cart"))
    try:
        set_line(request.session, card, request.POST.get("quantity", "1"))
    except CartError as exc:
        messages.error(request, f"{card.display_name}: {exc}")
        return redirect(next_url or reverse("public_catalog_part", args=[public_id]))
    if next_url == reverse("public_catalog_cart"):
        messages.success(request, "Количество обновлено.")
    elif card.in_stock:
        messages.success(request, f"{card.display_name}: добавлено в корзину.")
    else:
        messages.success(
            request, f"{card.display_name}: добавлено в корзину как запрос о поставке."
        )
    return redirect(next_url or reverse("public_catalog_cart"))


@require_POST
@never_cache
def public_cart_remove(request, public_id):
    remove_line(request.session, public_id)
    messages.success(request, "Позиция убрана из корзины.")
    return redirect("public_catalog_cart")


# --- Sending the cart as a customer request ----------------------------------------------


def _request_form(
    request, cart, *, token, values=None, error="", error_field=None, status=200
):
    return _render(
        request,
        "public_catalog/request_form.html",
        {
            "cart": cart,
            "line_inquiry": LINE_INQUIRY,
            "submission_key": token,
            "values": values or {},
            "error": error,
            # The field at fault is marked aria-invalid and points at the message.
            "error_field": error_field,
            "honeypot_field": public_requests.HONEYPOT_FIELD,
        },
        status=status,
    )


def _cart_blocks_request(request, cart):
    """A redirect back to the cart when this cart cannot be sent yet."""
    if cart.is_empty:
        return redirect("public_catalog_cart")
    if cart.short_lines:
        messages.error(
            request,
            "В корзине есть детали, которых сейчас меньше, чем выбрано. Уменьшите количество.",
        )
        return redirect("public_catalog_cart")
    return None


@require_safe
@never_cache
def public_request_form(request):
    cart = build_cart_view(request.session)
    blocked = _cart_blocks_request(request, cart)
    if blocked:
        return blocked
    return _request_form(request, cart, token=public_requests.form_token(request.session, cart))


@require_POST
@never_cache
def public_request_submit(request):
    submission = public_requests.matching_submission(
        request.session, request.POST.get("submission_key", "")
    )
    if submission is None:
        messages.error(request, "Форма устарела. Проверьте заявку и отправьте её ещё раз.")
        return redirect("public_catalog_request_form")
    if submission.request:
        # A second click or a browser retry of a form that already worked.
        return redirect("public_catalog_request_success", public_id=submission.request)

    cart = build_cart_view(request.session)
    blocked = _cart_blocks_request(request, cart)
    if blocked:
        return blocked
    values = {name: str(request.POST.get(name, "")) for name in public_requests.FORM_FIELDS}
    if public_requests.cart_fingerprint(cart) != submission.cart:
        return _request_form(
            request,
            cart,
            token=public_requests.form_token(request.session, cart),
            values=values,
            error="Корзина изменилась. Проверьте позиции и отправьте заявку ещё раз.",
            status=409,
        )

    def refuse(error, status=400, field=None):
        return _request_form(
            request,
            cart,
            token=submission.token,
            values=values,
            error=error,
            error_field=field,
            status=status,
        )

    if public_requests.looks_automated(request.POST):
        return refuse("Не удалось отправить заявку. Проверьте поля формы.")
    if request.POST.get("consent") != "1":
        return refuse("Подтвердите согласие на обработку персональных данных.", field="consent")
    try:
        public_requests.check_rate(request)
        public_id, _created = public_requests.send_cart(request, cart, submission, values)
    except public_requests.RequestRefused as exc:
        return refuse(str(exc), status=429)
    except CustomerRequestError as exc:
        return refuse(str(exc), field=exc.field)
    except BusinessWriteBlocked:
        return refuse("Приём заявок временно приостановлен. Попробуйте позже.", status=503)
    return redirect("public_catalog_request_success", public_id=public_id)


@require_safe
@never_cache
def public_request_success(request, public_id):
    """Shown only to the browser that sent this request; nothing is read back."""
    submission = public_requests.stored_submission(request.session)
    if submission is None or submission.request != str(public_id):
        raise Http404
    return _render(
        request,
        "public_catalog/request_success.html",
        {"reference": public_requests.request_reference(public_id)},
    )


@require_safe
def robots_txt(request):
    response = HttpResponse(public_seo.robots_txt(request), content_type="text/plain")
    patch_cache_control(response, public=True, max_age=CRAWLER_FILE_MAX_AGE)
    return response


@require_safe
def sitemap_index(request):
    pages = [
        public_seo.absolute_url(request, reverse("public_catalog_sitemap_parts", args=[number]))
        for number in range(1, public_seo.sitemap_page_count() + 1)
    ]
    response = render(
        request,
        "public_catalog/sitemap_index.xml",
        {"pages": pages},
        content_type="application/xml",
    )
    patch_cache_control(response, public=True, max_age=CRAWLER_FILE_MAX_AGE)
    return response


@require_safe
def sitemap_parts(request, number):
    if number < 1 or number > public_seo.sitemap_page_count():
        raise Http404
    base = public_seo.base_url(request)
    urls = [
        base + reverse("public_catalog_part", args=[public_id])
        for public_id in public_seo.sitemap_public_ids(number)
    ]
    if number == 1:
        urls.insert(0, base + reverse("public_catalog_root"))
    response = render(
        request, "public_catalog/sitemap.xml", {"urls": urls}, content_type="application/xml"
    )
    patch_cache_control(response, public=True, max_age=CRAWLER_FILE_MAX_AGE)
    return response


@require_safe
@never_cache
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - readiness must fail closed
        return JsonResponse({"status": "down", "db": "down"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})


# --- Error pages -----------------------------------------------------------------------
#
# None of them touches the database or the session: the failure being reported
# may be the database itself. They never show a traceback, SQL, a path or a
# model name.


def _error(request, template, status):
    return render(request, template, {"indexing": False}, status=status)


def bad_request(request, exception=None):
    return _error(request, "public_catalog/errors/400.html", 400)


def permission_denied(request, exception=None):
    return _error(request, "public_catalog/errors/400.html", 403)


def not_found(request, exception=None):
    return _error(request, "public_catalog/errors/404.html", 404)


def server_error(request):
    return _error(request, "public_catalog/errors/500.html", 500)


def csrf_failure(request, reason=""):
    return _error(request, "public_catalog/errors/csrf.html", 403)
