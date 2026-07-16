"""Layer 31 — экраны BRP-каталога: поиск, продвижение в склад, настройки цен.

Порядок поиска: сначала фактический склад (карточки + остатки по ячейкам),
затем BRP-справочник. Просмотр каталога доступен всем ролям; «Добавить в
склад» — can_manage_parts; «Учесть наличие» — can_manage_inventory; настройки
цен — can_manage_parts. Сам просмотр ничего не меняет.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.catalog.models import normalize_number
from apps.catalog.services import get_current_price_settings
from apps.core.part_lookup import resolve_part_lookup

from .models import BrpCatalogPart, BrpPartLink
from .pricing import customer_price_rub
from .services import (
    BrpPromotionError,
    find_promoted_part,
    get_or_create_intake_draft,
    promote_to_warehouse,
)

WAREHOUSE_LIMIT = 20
BRP_LIMIT = 50

STATUS_PILLS = {
    "OBS": "pill--danger",
    "USE": "pill--warning",
    "VIN": "pill--info",
    "LIQ": "pill--warning",
}


def _warehouse_matches(q: str, norm: str) -> list:
    """Карточки склада по номеру/штрихкоду/названию + остатки по ячейкам."""
    lookup = resolve_part_lookup(
        q,
        allow_partial=True,
        allow_name=True,
        allow_alias=True,
    )
    return [
        {
            "part": candidate.part,
            "balances": candidate.location_rows,
            "available": candidate.available,
            "physical": candidate.physical,
            "link": getattr(candidate.part, "brp_link", None),
        }
        for candidate in lookup.candidates[:WAREHOUSE_LIMIT]
    ]


def _brp_matches(q: str, norm: str, status: str, settings) -> list:
    """Позиции справочника: точный номер/замена, затем описание."""
    number_q = Q()
    if norm:
        number_q = (
            Q(material_no_norm=norm)
            | Q(replacement_no_1_norm=norm)
            | Q(replacement_no_2_norm=norm)
        )
    query = number_q
    if len(q) >= 3:
        query = query | Q(part_desc__icontains=q)
    qs = BrpCatalogPart.objects.filter(query)
    if status:
        qs = qs.filter(brp_status=status)
    parts = list(qs.order_by("material_no")[:BRP_LIMIT])
    promoted = {
        link.brp_part_id: link.part
        for link in BrpPartLink.objects.filter(brp_part__in=parts).select_related("part")
    }
    rows = []
    for brp in parts:
        rows.append({
            "brp": brp,
            "customer_price": customer_price_rub(
                brp.retail_price_usd,
                settings.current_usd_rate,
                settings.brp_markup_percent,
            ),
            "promoted_part": promoted.get(brp.pk),
            "status_pill": STATUS_PILLS.get(brp.brp_status, "pill--muted"),
        })
    return rows


@login_required
def brp_search(request):
    q = (request.GET.get("q") or "").strip()
    status = (request.GET.get("status") or "").strip()
    norm = normalize_number(q)
    pricing = get_current_price_settings()
    context = {
        "q": q,
        "status": status,
        "statuses": sorted(BrpCatalogPart.STATUS_LABELS.items()),
        "pricing": pricing,
        "catalog_size": BrpCatalogPart.objects.count(),
        "can_manage_parts": request.user.can_manage_parts,
        "can_manage_inventory": request.user.can_manage_inventory,
        "warehouse_rows": [],
        "brp_rows": [],
    }
    if len(q) >= 2 or status:
        if len(q) >= 2:
            context["warehouse_rows"] = _warehouse_matches(q, norm)
        context["brp_rows"] = _brp_matches(q, norm, status, pricing)
        context["searched"] = True
    return render(request, "brp/search.html", context)


@login_required
@require_POST
def brp_promote(request, pk):
    """«Добавить в склад»: только карточка, остатков не создаёт."""
    if not request.user.can_manage_parts:
        raise PermissionDenied
    brp_part = get_object_or_404(BrpCatalogPart, pk=pk)
    try:
        part = promote_to_warehouse(brp_part, by=request.user)
    except BrpPromotionError as exc:
        messages.error(request, str(exc))
        return redirect(f"{request.POST.get('next') or '/brp/'}")
    messages.success(
        request,
        f"Карточка «{part}» создана из BRP-каталога. Остатков пока нет: "
        "учтите наличие поступлением.",
    )
    return redirect("part_detail", pk=part.pk)


@login_required
@require_POST
def brp_intake(request, pk):
    """«Учесть наличие»: карточка (если нужно) + черновик начальных остатков."""
    if not request.user.can_manage_inventory:
        raise PermissionDenied
    brp_part = get_object_or_404(BrpCatalogPart, pk=pk)
    part = find_promoted_part(brp_part)
    if part is None:
        if not request.user.can_manage_parts:
            messages.error(
                request,
                "Этой детали ещё нет в складской картотеке. Создание карточек "
                "доступно администратору и руководителю: попросите добавить её.",
            )
            return redirect(f"/brp/?q={brp_part.material_no}")
        try:
            part = promote_to_warehouse(brp_part, by=request.user)
        except BrpPromotionError as exc:
            messages.error(request, str(exc))
            return redirect("brp_search")
    draft = get_or_create_intake_draft(by=request.user)
    messages.success(
        request,
        f"Деталь «{part}» подставлена в черновик {draft.number}: укажите "
        "количество, ячейку и цену, затем проведите документ.",
    )
    return redirect(f"/receipts/{draft.pk}/?new_part={part.pk}")


@login_required
def brp_settings(request):
    """Legacy BRP settings URL. The price settings screen is unified."""
    if not request.user.can_manage_parts:
        raise PermissionDenied
    return redirect("price_settings")
