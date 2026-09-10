"""Internal operator screens for public customer requests."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render

from apps.catalog.public_contracts import resolve_current_customer_price
from apps.inventory.availability import available_totals
from apps.inventory.presentation import with_part_identity

from .models import CustomerRequest
from .services import CustomerRequestError, change_request_status

PAGE_SIZE = 50


def _require_access(request) -> None:
    if not request.user.can_manage_sales:
        raise PermissionDenied


@login_required
def customer_request_list(request):
    _require_access(request)
    queryset = CustomerRequest.objects.annotate(line_count=Count("lines")).order_by(
        "-created_at", "-pk"
    )
    page_obj = Paginator(queryset, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "customer_requests/list.html",
        {"page_obj": page_obj, "new_count": CustomerRequest.objects.filter(status="new").count()},
    )


@login_required
def customer_request_detail(request, pk):
    _require_access(request)
    customer_request = get_object_or_404(CustomerRequest, pk=pk)
    lines = list(
        with_part_identity(
            customer_request.lines.select_related("part_type", "part_type__unit"),
            part_field="part_type",
        )
    )
    availability = available_totals(line.part_type_id for line in lines)
    for line in lines:
        line.current_available = availability[line.part_type_id]
        line.current_price = resolve_current_customer_price(line.part_type).price_rub
    events = customer_request.status_events.select_related("changed_by")
    return render(
        request,
        "customer_requests/detail.html",
        {"customer_request": customer_request, "lines": lines, "events": events},
    )


@login_required
def customer_request_status(request, pk):
    _require_access(request)
    if request.method != "POST":
        raise PermissionDenied
    target_status = (request.POST.get("status") or "").strip()
    try:
        customer_request, changed = change_request_status(
            request_id=pk, target_status=target_status, by=request.user
        )
    except CustomerRequest.DoesNotExist:
        customer_request = get_object_or_404(CustomerRequest, pk=pk)
    except CustomerRequestError as exc:
        messages.error(request, str(exc))
    else:
        if changed:
            messages.success(request, "Статус заявки обновлён.")
    return redirect("customer_request_detail", pk=customer_request.pk)
