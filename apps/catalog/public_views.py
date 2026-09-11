"""Unauthenticated runtime probes for the future public catalog.

There is deliberately no browse/search UI in Stage 3.  These endpoints prove
the separate runtime boundary without exposing an internal screen or DTO.
"""

from django.db import connection
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from .models import PartAnalog, PartCompatibility, PartType
from .public_contracts import build_public_part_facts
from .search import clean_query, search_parts

APPLICATIONS = ("ГИДРОЦИКЛ", "КВАДРОЦИКЛ", "СНЕГОХОД", "ЛОДОЧНЫЙ МОТОР", "КАТЕР")
CART_SESSION_KEY = "public_catalog_cart"
MAX_CART_LINES = 50
MAX_CART_QUANTITY = 1000


def _cart(request):
    raw = request.session.get(CART_SESSION_KEY, {})
    return raw if isinstance(raw, dict) else {}


def _cart_facts(request):
    cart = _cart(request)
    parts = PartType.objects.filter(public_id__in=cart, is_public=True)
    by_id = {str(part.public_id): part.pk for part in parts}
    facts = {str(fact.public_id): fact for fact in build_public_part_facts(by_id.values())}
    return [(facts[key], quantity) for key, quantity in cart.items() if key in facts]


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
            ).values_list("part_id", flat=True).distinct()
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
def public_cart(request):
    return render(request, "public_catalog/cart.html", {"lines": _cart_facts(request)})


@require_POST
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
    return redirect("public_catalog_cart")


@require_POST
def public_cart_remove(request, public_id):
    cart = _cart(request)
    cart.pop(str(public_id), None)
    request.session[CART_SESSION_KEY] = cart
    return redirect("public_catalog_cart")


@require_GET
def healthz(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception:  # noqa: BLE001 - readiness must fail closed
        return JsonResponse({"status": "down", "db": "down"}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})
