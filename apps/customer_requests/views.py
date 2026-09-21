"""Internal operator screens and the narrow Telegram and MAX webhook endpoints."""
import hmac
import json
import logging
import uuid
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.db import DatabaseError, transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import urlencode
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.catalog.public_contracts import resolve_current_customer_prices
from apps.inventory.availability import available_totals
from apps.inventory.presentation import with_part_identity
from apps.operations.models import TelegramBotRuntime
from apps.operations.write_guard import BusinessWriteBlocked

from . import max_bot, messaging, operator_console, operator_replies, workspace
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
    StaffMessengerBinding,
    TelegramConversation,
    TelegramDelivery,
    TelegramDeliveryStatus,
    TelegramMessage,
    TelegramOperator,
    TelegramOutboxEvent,
    WorkspaceEvent,
)
from .services import (
    CustomerRequestError,
    change_request_status,
    delete_all_cancelled_requests,
    delete_cancelled_request,
)
from .telegram import handle_update, webhook_secret_is_valid

PAGE_SIZE = 30
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
        operator.reply_request = None
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
def staff_messenger_bindings(request):
    _require_admin(request)
    token = None
    error = ""
    if request.method == "POST":
        action = request.POST.get("action")
        if action == "pair":
            user = get_object_or_404(get_user_model(), pk=request.POST.get("user_id"))
            try:
                token = operator_console.issue_pairing_token(
                    user=user,
                    label=request.POST.get("label", ""),
                    created_by=request.user,
                )
            except ValueError as exc:
                error = str(exc)
        elif action == "toggle":
            binding = get_object_or_404(StaffMessengerBinding, pk=request.POST.get("binding_id"))
            if binding.is_active:
                operator_console.revoke_binding(binding=binding)
                messages.success(
                    request,
                    "Привязка отозвана. Для повторного подключения создайте новый код.",
                )
            return redirect("staff_messenger_bindings")
    users = list(
        get_user_model()
        .objects.filter(is_active=True)
        .order_by("full_name", "username")
    )
    bindings = list(
        StaffMessengerBinding.objects.select_related("user").order_by(
            "provider", "user_id"
        )
    )
    bindings_by_user = {}
    for binding in bindings:
        bindings_by_user.setdefault(binding.user_id, []).append(binding)
    staff_statuses = []
    for user in users:
        user_bindings = bindings_by_user.get(user.pk, [])
        active_by_provider = {
            provider: any(
                binding.is_active and binding.provider == provider
                for binding in user_bindings
            )
            for provider in StaffMessengerBinding.Provider.values
        }
        label = next(
            (
                binding.customer_visible_label
                for binding in user_bindings
                if binding.customer_visible_label
            ),
            str(user),
        ) or str(user)
        staff_statuses.append(
            {
                "label": label,
                "telegram_connected": active_by_provider[StaffMessengerBinding.Provider.TELEGRAM],
                "max_connected": active_by_provider[StaffMessengerBinding.Provider.MAX],
                "bindings": user_bindings,
            }
        )
    return render(
        request,
        "customer_requests/staff_bindings.html",
        {
            "bindings": bindings,
            "staff_statuses": staff_statuses,
            "users": users,
            "token": token,
            "error": error,
            "providers": StaffMessengerBinding.Provider.choices,
        },
    )


def _list_params(source) -> dict:
    """The list's own filters, cleaned: anything else in a query string is dropped."""
    return {
        "tab": workspace.clean_tab(source.get("tab")),
        "messenger": workspace.clean_messenger(source.get("messenger")),
        "q": workspace.clean_query(source.get("q")),
    }


def _list_query(params: dict, **changes) -> str:
    """The filters as a query string; the defaults stay out of the URL."""
    values = {**params, **changes}
    if values.get("tab") == workspace.TAB_ACTIVE:
        values.pop("tab")
    return urlencode({key: value for key, value in values.items() if value})


def _detail_url(pk, params: dict) -> str:
    query = _list_query(params)
    url = reverse("customer_request_detail", args=[pk])
    return f"{url}?{query}" if query else url


@login_required
def customer_request_list(request):
    _require_access(request)
    params = _list_params(request.GET)
    queryset = workspace.filtered_requests(
        tab=params["tab"], messenger=params["messenger"], query=params["q"]
    )

    paginator = Paginator(queryset, PAGE_SIZE)
    page_obj = paginator.get_page(request.GET.get("page"))
    for item in page_obj.object_list:
        item.total = workspace.request_total(
            known_total=item.known_total, unknown_price_count=item.unknown_price_count
        )
    counts = workspace.tab_counts(messenger=params["messenger"], query=params["q"])
    tabs = [
        {
            "value": value,
            "label": label,
            "count": counts[value],
            "active": value == params["tab"],
            "query": _list_query(params, tab=value),
        }
        for value, label in workspace.TABS
    ]
    messengers = [
        {
            "value": value,
            "label": label,
            "active": value == params["messenger"],
            "query": _list_query(params, messenger=value),
        }
        for value, label in workspace.MESSENGERS
    ]
    return render(
        request,
        "customer_requests/list.html",
        {
            "page_obj": page_obj,
            "is_paginated": page_obj.has_other_pages(),
            "params": params,
            "list_query": _list_query(params),
            "tabs": tabs,
            "messengers": messengers,
            "waiting_count": counts[workspace.TAB_WAITING],
            "active_tab_label": dict(workspace.TABS)[params["tab"]],
            "workspace_cursor": (
                WorkspaceEvent.objects.order_by("-event_id")
                .values_list("event_id", flat=True)
                .first()
                or 0
            ),
        },
    )


@login_required
def customer_request_events(request):
    """Bounded replay endpoint for the polling workspace cursor."""
    _require_access(request)
    raw = request.GET.get("after", "0")
    try:
        after = int(raw)
    except (TypeError, ValueError):
        return JsonResponse({"error": "Некорректный курсор."}, status=400)
    if after < 0:
        return JsonResponse({"error": "Некорректный курсор."}, status=400)
    events = WorkspaceEvent.objects.filter(event_id__gt=after).order_by("event_id")[:100]
    rows = [
        {"id": event.event_id, "type": event.event_type, "payload": event.payload}
        for event in events
    ]
    return JsonResponse({"events": rows, "cursor": rows[-1]["id"] if rows else after})


def _detail_context(
    customer_request,
    *,
    params: dict,
    telegram_start_link=None,
    max_start_link=None,
    reply_text="",
):
    lines = list(
        with_part_identity(
            customer_request.lines.select_related("part_type", "part_type__unit"),
            part_field="part_type",
        )
    )
    availability = available_totals(line.part_type_id for line in lines)
    prices = resolve_current_customer_prices({line.part_type for line in lines})
    for line in lines:
        line.current_available = availability[line.part_type_id]
        line.current_price = prices[line.part_type_id].price_rub
        line.line_total = (
            None if line.price_seen is None else line.price_seen * line.quantity_requested
        )
    target = operator_replies.reply_target(customer_request)
    list_query = _list_query(params)
    return {
        "customer_request": customer_request,
        "lines": lines,
        "total": workspace.lines_total(lines),
        "events": customer_request.status_events.select_related("changed_by"),
        "timeline": workspace.timeline(customer_request),
        "reply_target": target,
        "reply_blocked": target.blocked_reason(),
        # A link is offered only while there is nobody to answer yet.
        "can_issue_link": (
            not target.linked
            and customer_request.status in workspace.OPEN_STATUSES
            and messaging.customer_contact_allowed(customer_request)
        ),
        "reply_text": reply_text,
        # A fresh key per rendered form: a double submit queues one reply.
        "reply_key": uuid.uuid4().hex,
        "telegram_start_link": telegram_start_link,
        "max_start_link": max_start_link,
        "list_query": list_query,
        "back_url": reverse("customer_request_list") + (f"?{list_query}" if list_query else ""),
        "action_query": f"?{list_query}" if list_query else "",
        "workspace_cursor": (
            WorkspaceEvent.objects.order_by("-event_id").values_list("event_id", flat=True).first()
            or 0
        ),
    }


def _annotated_or_404(pk) -> CustomerRequest:
    customer_request = workspace.annotated_request(pk)
    if customer_request is None:
        raise Http404
    return customer_request


@login_required
def customer_request_detail(request, pk):
    _require_access(request)
    customer_request = _annotated_or_404(pk)
    return render(
        request,
        "customer_requests/detail.html",
        _detail_context(customer_request, params=_list_params(request.GET)),
    )


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
    return redirect(_detail_url(pk, _list_params(request.GET)))


@login_required
@require_POST
def customer_request_delete(request, pk):
    _require_access(request)
    try:
        delete_cancelled_request(request_id=pk, by=request.user)
    except CustomerRequest.DoesNotExist:
        raise Http404 from None
    except CustomerRequestError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Заявка удалена.")
    return redirect(reverse("customer_request_list") + "?tab=canceled")


@login_required
@require_POST
def customer_request_delete_all(request):
    _require_access(request)
    deleted = delete_all_cancelled_requests(by=request.user)
    messages.success(request, f"Удалено отменённых заявок: {deleted}.")
    return redirect(reverse("customer_request_list") + "?tab=canceled")


@login_required
@require_POST
def customer_request_telegram_link(request, pk):
    _require_access(request)
    customer_request = _annotated_or_404(pk)
    params = _list_params(request.GET)
    try:
        issued = issue_telegram_link(request_id=customer_request.pk, by=request.user)
        start_link = telegram_start_url(issued.token)
    except MessengerLinkError as exc:
        messages.error(request, str(exc))
        return redirect(_detail_url(customer_request.pk, params))
    if start_link is None:
        messages.error(request, "Telegram-бот ещё не настроен.")
        return redirect(_detail_url(customer_request.pk, params))
    return render(
        request,
        "customer_requests/detail.html",
        _detail_context(customer_request, params=params, telegram_start_link=start_link),
    )


@login_required
@require_POST
def customer_request_max_link(request, pk):
    _require_access(request)
    customer_request = _annotated_or_404(pk)
    params = _list_params(request.GET)
    try:
        issued = issue_max_link(request_id=customer_request.pk, by=request.user)
        start_link = max_start_url(issued.token)
    except MessengerLinkError as exc:
        messages.error(request, str(exc))
        return redirect(_detail_url(customer_request.pk, params))
    if start_link is None:
        messages.error(request, "MAX-бот ещё не настроен.")
        return redirect(_detail_url(customer_request.pk, params))
    return render(
        request,
        "customer_requests/detail.html",
        _detail_context(customer_request, params=params, max_start_link=start_link),
    )


@login_required
@require_POST
def customer_request_reply(request, pk):
    """An employee answers the customer from DenisStock, as the PRO-STOR bot.

    One form for both messengers: the request's own messenger is chosen here,
    never by the page. A refused reply keeps the typed text on the page.
    """
    _require_access(request)
    customer_request = _annotated_or_404(pk)
    params = _list_params(request.GET)
    text = request.POST.get("text", "")
    try:
        result = operator_replies.submit_reply(
            request_id=customer_request.pk,
            user=request.user,
            text=text,
            key=request.POST.get("submission_key", ""),
            attachment=request.FILES.get("attachment"),
        )
    except operator_replies.OperatorReplyError as exc:
        messages.error(request, str(exc))
        customer_request = _annotated_or_404(pk)
        return render(
            request,
            "customer_requests/detail.html",
            _detail_context(customer_request, params=params, reply_text=text),
        )
    if result.created:
        messages.success(request, f"Ответ поставлен в отправку клиенту в {result.label}.")
    return redirect(_detail_url(customer_request.pk, params))


# The MAX-only reply of the previous release posts here; it is the same reply now.
customer_request_max_reply = customer_request_reply


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
            max_bot.handle_update(
                update,
                attachment_loader=lambda body: max_bot.load_operator_attachment(
                    max_bot.build_api(), body
                ),
            )
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
