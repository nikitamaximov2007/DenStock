"""Public catalog and the deliberately narrow anonymous request hand-off."""

from __future__ import annotations

import hmac
import secrets
from uuid import UUID

from django.db import connection
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from apps.customer_requests.models import CustomerRequest
from apps.customer_requests.policies import current_consent_versions
from apps.customer_requests.services import (
    CustomerRequestError,
    RequestLineInput,
    create_customer_request,
)

from .models import PartAnalog, PartCompatibility, PartType
from .public_contracts import build_public_part_facts
from .search import clean_query, search_parts

APPLICATIONS = ("ГИДРОЦИКЛ", "КВАДРОЦИКЛ", "СНЕГОХОД", "ЛОДОЧНЫЙ МОТОР", "КАТЕР")
CART_SESSION_KEY = "public_catalog_cart"
REQUEST_DRAFT_SESSION_KEY = "public_catalog_request_draft"
REQUEST_SUBMISSION_SESSION_KEY = "public_catalog_request_submission"
MAX_CART_LINES = 50
MAX_CART_QUANTITY = 1000


def _cart(request):
    raw = request.session.get(CART_SESSION_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _clear_request_draft(request) -> None:
    """A changed cart is a new logical submission, never a retry."""
    request.session.pop(REQUEST_DRAFT_SESSION_KEY, None)
    request.session.pop(REQUEST_SUBMISSION_SESSION_KEY, None)


def _cart_facts(request):
    cart = _cart(request)
    parts = PartType.objects.filter(public_id__in=cart, is_public=True)
    by_id = {str(part.public_id): part.pk for part in parts}
    facts = {str(fact.public_id): fact for fact in build_public_part_facts(by_id.values())}
    return [(facts[key], quantity) for key, quantity in cart.items() if key in facts]


def _supply_fact(public_id):
    try:
        public_id = UUID(str(public_id))
    except (TypeError, ValueError):
        return None
    part = PartType.objects.filter(public_id=public_id, is_public=True).first()
    if part is None:
        return None
    facts = build_public_part_facts([part.pk])
    return facts[0] if facts else None


def _set_request_draft(request, supply_public_id: str | None = None) -> dict:
    draft = (
        {"kind": "supply", "public_id": supply_public_id} if supply_public_id else {"kind": "cart"}
    )
    if request.session.get(REQUEST_DRAFT_SESSION_KEY) != draft:
        request.session[REQUEST_DRAFT_SESSION_KEY] = draft
        request.session[REQUEST_SUBMISSION_SESSION_KEY] = {
            "token": secrets.token_urlsafe(32),
            "draft": draft,
        }
    return draft


def _request_display_lines(request, draft):
    if draft.get("kind") == "supply":
        fact = _supply_fact(draft.get("public_id"))
        return [(fact, 1, True)] if fact else []
    return [(fact, quantity, False) for fact, quantity in _cart_facts(request)]


def _request_line_inputs(request, draft):
    display_lines = _request_display_lines(request, draft)
    public_ids = [str(fact.public_id) for fact, _quantity, _supply in display_lines]
    parts = {
        str(public_id): pk
        for public_id, pk in (
            PartType.objects.filter(public_id__in=public_ids, is_public=True).values_list(
                "public_id", "pk"
            )
        )
    }
    if len(parts) != len(display_lines):
        raise CustomerRequestError("Одна или несколько деталей больше недоступны.")
    return [
        RequestLineInput(
            part_id=parts[str(fact.public_id)], quantity=quantity, supply_inquiry=supply_inquiry
        )
        for fact, quantity, supply_inquiry in display_lines
    ]


def _request_context(request, *, draft, error="", values=None):
    submission = request.session.get(REQUEST_SUBMISSION_SESSION_KEY, {})
    return {
        "lines": _request_display_lines(request, draft),
        "submission_key": submission.get("token", ""),
        "error": error,
        "values": values or {},
        "privacy_policy_version": current_consent_versions()[0],
        "consent_version": current_consent_versions()[1],
    }


@require_GET
def public_root(request):
    return render(request, "public_catalog/home.html")


@require_GET
def public_search(request):
    query = clean_query(request.GET.get("q"))
    page = search_parts(query, page=request.GET.get("page", 1)) if query else None
    part_ids = [hit.part_id for hit in page.hits] if page else []
    application = request.GET.get("application", "")
    manufacturer = request.GET.get("manufacturer", "")
    if application in APPLICATIONS:
        part_ids = list(
            PartCompatibility.objects.filter(
                part_id__in=part_ids,
                vehicle_model__vehicle_make__vehicle_type__name=application,
            )
            .values_list("part_id", flat=True)
            .distinct()
        )
    if manufacturer:
        part_ids = list(
            PartType.objects.filter(pk__in=part_ids, manufacturer__name=manufacturer).values_list(
                "pk", flat=True
            )
        )
    facts = build_public_part_facts(part_ids)
    if request.GET.get("in_stock") == "1":
        facts = [fact for fact in facts if fact.available_quantity > 0]
    manufacturers = sorted({fact.manufacturer for fact in facts if fact.manufacturer})
    return render(
        request,
        "public_catalog/search.html",
        {
            "query": query,
            "page": page,
            "facts": facts,
            "applications": APPLICATIONS,
            "application": application,
            "manufacturer": manufacturer,
            "manufacturers": manufacturers,
            "in_stock": request.GET.get("in_stock") == "1",
        },
    )


@require_GET
def public_part_detail(request, public_id):
    part = PartType.objects.filter(public_id=public_id, is_public=True).first()
    if part is None:
        raise Http404
    facts = build_public_part_facts([part.pk])[0]
    links = PartAnalog.objects.filter(original=part, is_confirmed=True, analog__is_public=True)
    analogs = build_public_part_facts(links.values_list("analog_id", flat=True))
    return render(
        request,
        "public_catalog/part_detail.html",
        {
            "facts": facts,
            "analogs": analogs,
            "canonical_path": reverse("public_catalog_part", args=[facts.public_id]),
            "cart_count": sum(_cart(request).values()),
        },
    )


@require_GET
def robots_txt(request):
    return HttpResponse("User-agent: *\nAllow: /\nDisallow: /search/\n", content_type="text/plain")


@require_GET
def sitemap_xml(request):
    public_ids = PartType.objects.filter(is_public=True).values_list("public_id", flat=True)
    paths = [reverse("public_catalog_part", args=[public_id]) for public_id in public_ids]
    return render(
        request,
        "public_catalog/sitemap.xml",
        {"paths": paths},
        content_type="application/xml",
    )


@require_GET
@never_cache
def public_cart(request):
    return render(request, "public_catalog/cart.html", {"lines": _cart_facts(request)})


@require_POST
@never_cache
def public_cart_add(request, public_id):
    part = PartType.objects.filter(public_id=public_id, is_public=True).first()
    if part is None:
        raise Http404
    try:
        quantity = int(request.POST.get("quantity", "1"))
    except ValueError:
        quantity = 0
    facts = build_public_part_facts([part.pk])[0]
    if quantity < 1 or quantity > MAX_CART_QUANTITY or facts.available_quantity < quantity:
        return redirect("public_catalog_part", public_id=public_id)
    cart = _cart(request)
    key = str(public_id)
    if key not in cart and len(cart) >= MAX_CART_LINES:
        return redirect("public_catalog_cart")
    cart[key] = quantity
    request.session[CART_SESSION_KEY] = cart
    _clear_request_draft(request)
    return redirect("public_catalog_cart")


@require_POST
@never_cache
def public_cart_remove(request, public_id):
    cart = _cart(request)
    cart.pop(str(public_id), None)
    request.session[CART_SESSION_KEY] = cart
    _clear_request_draft(request)
    return redirect("public_catalog_cart")


@require_GET
@never_cache
def public_request_form(request):
    supply_fact = _supply_fact(request.GET.get("supply")) if request.GET.get("supply") else None
    if request.GET.get("supply") and supply_fact is None:
        raise Http404
    draft = _set_request_draft(
        request, str(supply_fact.public_id) if supply_fact is not None else None
    )
    context = _request_context(request, draft=draft)
    if not context["lines"]:
        return redirect("public_catalog_cart")
    return render(request, "public_catalog/request_form.html", context)


@require_POST
@never_cache
def public_request_submit(request):
    submission = request.session.get(REQUEST_SUBMISSION_SESSION_KEY, {})
    submitted_key = str(request.POST.get("submission_key") or "")
    expected_key = str(submission.get("token") or "")
    if not expected_key or not hmac.compare_digest(submitted_key, expected_key):
        return HttpResponseBadRequest("Некорректная отправка заявки.")
    draft = submission.get("draft")
    if not isinstance(draft, dict):
        return HttpResponseBadRequest("Некорректная отправка заявки.")

    values = {
        "customer_name": request.POST.get("customer_name", ""),
        "customer_phone": request.POST.get("customer_phone", ""),
        "preferred_messenger": request.POST.get("preferred_messenger", ""),
        "comment": request.POST.get("comment", ""),
    }
    if request.POST.get("consent") != "1":
        return render(
            request,
            "public_catalog/request_form.html",
            _request_context(
                request,
                draft=draft,
                error="Подтвердите согласие на обработку данных.",
                values=values,
            ),
            status=400,
        )
    try:
        privacy_policy_version, personal_data_consent_version = current_consent_versions()
        customer_request, created = create_customer_request(
            **values,
            lines=_request_line_inputs(request, draft),
            privacy_policy_version=privacy_policy_version,
            personal_data_consent_version=personal_data_consent_version,
            submission_key=submitted_key,
        )
    except CustomerRequestError as exc:
        return render(
            request,
            "public_catalog/request_form.html",
            _request_context(request, draft=draft, error=str(exc), values=values),
            status=400,
        )
    if created and draft.get("kind") == "cart":
        request.session.pop(CART_SESSION_KEY, None)
    return redirect("public_catalog_request_success", public_id=customer_request.public_id)


@require_GET
@never_cache
def public_request_success(request, public_id):
    if not CustomerRequest.objects.filter(public_id=public_id).exists():
        raise Http404
    return render(request, "public_catalog/request_success.html", {"public_id": public_id})


@require_GET
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - readiness must fail closed
        return JsonResponse({"status": "down", "db": "down"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
