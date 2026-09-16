"""Internal operator screens and the narrow Telegram and MAX webhook endpoints."""
import hmac
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.db.models import Count
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.catalog.public_contracts import resolve_current_customer_price
from apps.inventory.availability import available_totals
from apps.inventory.presentation import with_part_identity
from apps.operations.models import TelegramBotRuntime
from apps.operations.write_guard import BusinessWriteBlocked

from . import max_bot, max_service
from .forms import TelegramOperatorForm
from .max_api import webhook_secret_is_well_formed
from .messengers import (
    MessengerLinkError,
    issue_max_link,
    issue_telegram_link,
    max_start_url,
    telegram_start_url,
)
from .models import (
    CustomerRequest,
    MaxConversation,
    TelegramConversation,
    TelegramDelivery,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOperator,
    TelegramOutboxEvent,
)
from .services import CustomerRequestError, change_request_status
from .telegram import handle_update, webhook_secret_is_valid

PAGE_SIZE = 50
TELEGRAM_HISTORY_LIMIT = 200
MAX_HISTORY_LIMIT = 200
MAX_WEBHOOK_BODY_BYTES = 64 * 1024
logger = logging.getLogger(__name__)
BOT_ALIVE_WINDOW = timedelta(seconds=120)


def _require_access(request) -> None:
    if not request.user.can_manage_sales:
        raise PermissionDenied


def _require_admin(request) -> None:
    if not request.user.can_manage_users:
        raise PermissionDenied


def telegram_status() -> dict:
    """Safe diagnostics: counts and timestamps only, never the token."""
    runtime = TelegramBotRuntime.objects.filter(pk=TelegramBotRuntime.SINGLETON_PK).first()
    now = timezone.now()
    problem = [TelegramDeliveryStatus.FAILED, TelegramDeliveryStatus.UNCERTAIN]
    return {
        "runtime": runtime,
        "running": bool(runtime and runtime.heartbeat_at and now - runtime.heartbeat_at
                        < BOT_ALIVE_WINDOW and runtime.worker_id),
        "username": settings.TELEGRAM_BOT_USERNAME,
        "pending_events": TelegramOutboxEvent.objects.filter(
            status=TelegramOutboxEvent.Status.PENDING
        ).count(),
        "pending_customer": TelegramMessage.objects.filter(
            delivery_status=TelegramDeliveryStatus.PENDING
        ).count(),
        "pending_operator": TelegramDelivery.objects.filter(
            status=TelegramDeliveryStatus.PENDING
        ).count(),
        "failed_customer": TelegramMessage.objects.filter(delivery_status__in=problem).count(),
        "failed_operator": TelegramDelivery.objects.filter(status__in=problem).count(),
        "linked": TelegramConversation.objects.filter(
            status=TelegramConversation.Status.LINKED
        ).count(),
        "awaiting": TelegramConversation.objects.filter(
            status=TelegramConversation.Status.AWAITING_LINK
        ).count(),
    }


@login_required
def telegram_settings(request):
    _require_admin(request)
    form = TelegramOperatorForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        operator = form.save(commit=False)
        operator.created_by = request.user
        operator.save()
        messages.success(request, f"Сотрудник {operator.user} добавлен в Telegram-бота.")
        return redirect("telegram_settings")
    return render(
        request,
        "customer_requests/telegram_settings.html",
        {
            "form": form,
            "operators": TelegramOperator.objects.select_related("user").order_by("pk"),
            "status": telegram_status(),
            "roles": TelegramOperator.Role.choices,
        },
    )


@login_required
@require_POST
def telegram_operator_toggle(request, pk):
    _require_admin(request)
    operator = get_object_or_404(TelegramOperator, pk=pk)
    operator.is_active = not operator.is_active
    if not operator.is_active:
        operator.reply_conversation = None
        operator.reply_started_at = None
    operator.save()
    state = "включён" if operator.is_active else "отключён"
    messages.success(request, f"Доступ {operator.user} к боту {state}.")
    return redirect("telegram_settings")


@login_required
@require_POST
def telegram_operator_role(request, pk):
    _require_admin(request)
    operator = get_object_or_404(TelegramOperator, pk=pk)
    role = request.POST.get("role", "")
    if role not in TelegramOperator.Role.values:
        messages.error(request, "Неизвестная роль.")
    else:
        operator.role = role
        operator.save(update_fields=["role", "updated_at"])
        messages.success(request, "Роль сотрудника в боте обновлена.")
    return redirect("telegram_settings")


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


def _detail_context(customer_request, *, telegram_start_link=None, max_start_link=None):
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
    conversation = TelegramConversation.objects.filter(request=customer_request).first()
    telegram_messages = []
    if conversation is not None:
        # The newest 200, shown oldest first.
        telegram_messages = list(
            conversation.messages.select_related("operator_user").order_by("-created_at", "-pk")[
                :TELEGRAM_HISTORY_LIMIT
            ]
        )[::-1]
    max_conversation = MaxConversation.objects.filter(request=customer_request).first()
    max_messages = []
    if max_conversation is not None:
        max_messages = list(
            max_conversation.messages.select_related("operator_user").order_by(
                "-created_at", "-pk"
            )[:MAX_HISTORY_LIMIT]
        )[::-1]
    return {
        "customer_request": customer_request,
        "lines": lines,
        "events": events,
        "telegram_start_link": telegram_start_link,
        "telegram_conversation": conversation,
        "telegram_messages": telegram_messages,
        "max_start_link": max_start_link,
        "max_conversation": max_conversation,
        "max_messages": max_messages,
        # A fresh key per rendered form: a double submit queues one reply.
        "max_reply_key": uuid.uuid4().hex,
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
        _customer_request, changed = change_request_status(
            request_id=pk, target_status=target_status, by=request.user
        )
    except CustomerRequest.DoesNotExist:
        raise Http404 from None
    except CustomerRequestError as exc:
        # A stale page can offer a transition that is no longer allowed.
        messages.error(request, str(exc))
    else:
        if changed:
            messages.success(request, "Статус заявки обновлён.")
    return redirect("customer_request_detail", pk=pk)


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


@login_required
@require_POST
def customer_request_max_link(request, pk):
    _require_access(request)
    customer_request = get_object_or_404(CustomerRequest, pk=pk)
    try:
        issued = issue_max_link(request_id=customer_request.pk, by=request.user)
        start_link = max_start_url(issued.token)
    except MessengerLinkError as exc:
        messages.error(request, str(exc))
        return redirect("customer_request_detail", pk=customer_request.pk)
    if start_link is None:
        messages.error(request, "MAX-бот ещё не настроен.")
        return redirect("customer_request_detail", pk=customer_request.pk)
    return render(
        request,
        "customer_requests/detail.html",
        _detail_context(customer_request, max_start_link=start_link),
    )


@login_required
@require_POST
def customer_request_max_reply(request, pk):
    """An employee answers a MAX customer from DenisStock, as the PRO-STOR bot."""
    _require_access(request)
    customer_request = get_object_or_404(CustomerRequest, pk=pk)
    try:
        max_service.submit_operator_reply(
            request_id=customer_request.pk,
            user=request.user,
            text=request.POST.get("text", ""),
            submission_key=request.POST.get("submission_key", ""),
        )
    except max_service.OperatorReplyError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Ответ поставлен в отправку клиенту в MAX.")
    return redirect("customer_request_detail", pk=customer_request.pk)


def max_webhook_secret_is_valid(value: str | None) -> bool:
    configured = settings.MAX_WEBHOOK_SECRET
    return (
        bool(settings.MAX_WEBHOOK_ENABLED)
        and webhook_secret_is_well_formed(configured)
        and hmac.compare_digest((value or "").encode(), configured.encode())
    )


@csrf_exempt
@require_POST
def max_webhook(request):
    """Receive one MAX update: authenticate, store, answer 200 quickly.

    Nothing about requests or customers is ever in a response: every accepted
    update, whatever it did, answers the same ``{"ok": true}``. The payload
    and the secret header are never logged. A database fault answers 503 so
    MAX delivers again; everything stored is idempotent, so that is safe.
    """
    if not max_webhook_secret_is_valid(request.headers.get("X-Max-Bot-Api-Secret")):
        raise Http404
    if len(request.body) > MAX_WEBHOOK_BODY_BYTES:
        return HttpResponseBadRequest()
    try:
        update = json.loads(request.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return HttpResponseBadRequest()
    if not isinstance(update, dict) or not isinstance(update.get("update_type"), str):
        return HttpResponseBadRequest()
    try:
        with transaction.atomic():
            max_bot.handle_update(update)
    except BusinessWriteBlocked:
        return HttpResponse(status=503)
    except DatabaseError as exc:
        logger.warning("max webhook storage failed: %s", type(exc).__name__)
        return HttpResponse(status=503)
    except Exception as exc:  # noqa: BLE001 - one poisoned update must not be retried forever
        logger.error(
            "max webhook update %s failed: %s", update.get("update_type")[:32], type(exc).__name__
        )
    return JsonResponse({"ok": True})


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
