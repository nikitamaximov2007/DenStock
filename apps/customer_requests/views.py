"""Internal operator screens and the narrow Telegram webhook endpoint."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import Http404, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.catalog.public_contracts import resolve_current_customer_price
from apps.inventory.availability import available_totals
from apps.inventory.presentation import with_part_identity

from .messengers import MessengerLinkError, issue_telegram_link, telegram_start_url
from .models import CustomerRequest
from .services import CustomerRequestError, change_request_status
from .telegram import handle_update, webhook_secret_is_valid

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


def _detail_context(customer_request, *, telegram_start_link=None):
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
    return {
        "customer_request": customer_request,
        "lines": lines,
        "events": events,
        "telegram_start_link": telegram_start_link,
    }


@login_required
def customer_request_detail(request, pk):
    _require_access(request)
    customer_request = get_object_or_404(CustomerRequest, pk=pk)
    return render(request, "customer_requests/detail.html", _detail_context(customer_request))


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


@login_required
@require_POST
def customer_request_telegram_link(request, pk):
    _require_access(request)
    customer_request = get_object_or_404(CustomerRequest, pk=pk)
    try:
        issued = issue_telegram_link(request_id=customer_request.pk, by=request.user)
        start_link = telegram_start_url(issued.token)
    except MessengerLinkError as exc:
        messages.error(request, str(exc))
        return redirect("customer_request_detail", pk=customer_request.pk)
    if start_link is None:
        messages.error(request, "Telegram-бот ещё не настроен.")
        return redirect("customer_request_detail", pk=customer_request.pk)
    return render(
        request,
        "customer_requests/detail.html",
        _detail_context(customer_request, telegram_start_link=start_link),
    )


@csrf_exempt
@require_POST
def telegram_webhook(request):
    """Accept a bounded official Bot API update without logging its payload."""
    if not webhook_secret_is_valid(request.headers.get("X-Telegram-Bot-Api-Secret-Token")):
        raise Http404
    if len(request.body) > 32 * 1024:
        return HttpResponseBadRequest()
    try:
        update = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest()
    result = handle_update(update)
    return JsonResponse({"ok": True, "accepted": result.accepted})
