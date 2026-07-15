"""Polaris catalog screens."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.catalog.models import normalize_number
from apps.catalog.services import get_current_price_settings
from apps.core.part_lookup import resolve_part_lookup

from .models import PolarisCatalogPart, PolarisPartLink
from .pricing import customer_price_rub
from .services import (
    PolarisPromotionError,
    find_polaris_price_source,
    find_promoted_part,
    get_or_create_intake_draft,
    promote_to_warehouse,
)

WAREHOUSE_LIMIT = 20
POLARIS_LIMIT = 50


def _warehouse_matches(q: str, norm: str) -> list:
    lookup = resolve_part_lookup(q, allow_partial=True, allow_name=True)
    return [
        {
            "part": candidate.part,
            "balances": candidate.location_rows,
            "available": candidate.available,
            "physical": candidate.physical,
            "link": getattr(candidate.part, "polaris_link", None),
        }
        for candidate in lookup.candidates[:WAREHOUSE_LIMIT]
    ]


def _polaris_matches(q: str, norm: str, settings) -> list:
    number_q = Q(pk=None)
    if norm:
        number_q = Q(part_number_norm=norm) | Q(superseded_number_norm=norm)
    query = number_q
    if len(q) >= 3:
        query = query | Q(part_name__icontains=q)
    parts = list(PolarisCatalogPart.objects.filter(query).order_by("part_number")[:POLARIS_LIMIT])
    promoted = {
        link.polaris_part_id: link.part
        for link in PolarisPartLink.objects.filter(polaris_part__in=parts).select_related("part")
    }
    rows = []
    for polaris in parts:
        price_source = find_polaris_price_source(norm, polaris)
        retail = price_source.retail_price_usd if price_source else polaris.retail_price_usd
        rows.append({
            "polaris": polaris,
            "customer_price": customer_price_rub(
                retail,
                settings.current_usd_rate,
                settings.polaris_markup_percent,
            ),
            "price_source": (
                price_source if price_source and price_source.pk != polaris.pk else None
            ),
            "promoted_part": promoted.get(polaris.pk),
        })
    return rows


@login_required
def polaris_search(request):
    q = (request.GET.get("q") or "").strip()
    norm = normalize_number(q)
    pricing = get_current_price_settings()
    context = {
        "q": q,
        "pricing": pricing,
        "catalog_size": PolarisCatalogPart.objects.count(),
        "can_manage_parts": request.user.can_manage_parts,
        "can_manage_inventory": request.user.can_manage_inventory,
        "warehouse_rows": [],
        "polaris_rows": [],
    }
    if len(q) >= 2:
        context["warehouse_rows"] = _warehouse_matches(q, norm)
        context["polaris_rows"] = _polaris_matches(q, norm, pricing)
        context["searched"] = True
    return render(request, "polaris/search.html", context)


@login_required
@require_POST
def polaris_promote(request, pk):
    if not request.user.can_manage_parts:
        raise PermissionDenied
    polaris_part = get_object_or_404(PolarisCatalogPart, pk=pk)
    try:
        part = promote_to_warehouse(polaris_part, by=request.user)
    except PolarisPromotionError as exc:
        messages.error(request, str(exc))
        return redirect(request.POST.get("next") or "/polaris/")
    messages.success(
        request,
        f"Карточка «{part}» создана из Polaris-каталога. Остатков пока нет: "
        "учтите наличие поступлением.",
    )
    return redirect("part_detail", pk=part.pk)


@login_required
@require_POST
def polaris_intake(request, pk):
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    polaris_part = get_object_or_404(PolarisCatalogPart, pk=pk)
    part = find_promoted_part(polaris_part)
    if part is None:
        if not request.user.can_manage_parts:
            messages.error(
                request,
                "Этой детали ещё нет в складской картотеке. Создание карточек "
                "доступно администратору и руководителю: попросите добавить её.",
            )
            return redirect(f"/polaris/?q={polaris_part.part_number}")
        try:
            part = promote_to_warehouse(polaris_part, by=request.user)
        except PolarisPromotionError as exc:
            messages.error(request, str(exc))
            return redirect("polaris_search")
    draft = get_or_create_intake_draft(by=request.user)
    messages.success(
        request,
        f"Деталь «{part}» подставлена в черновик {draft.number}: укажите "
        "количество, ячейку и цену, затем проведите документ.",
    )
    return redirect(f"/receipts/{draft.pk}/?new_part={part.pk}")


@login_required
def polaris_settings(request):
    if not request.user.can_manage_parts:
        raise PermissionDenied
    return redirect("price_settings")
