"""Справочник клиентов. View - оркестратор, бизнес-логика в services.

Доступ повторяет существующую модель прав: карточка клиента нужна тем, кто
оформляет продажи, резервы или ремонты. Отдельного ACL не заводим.
"""

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID, uuid4

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods

from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

from .forms import CustomerForm
from .legacy_linking import legacy_group_summary, link_legacy_group, suggest_identity
from .models import Customer
from .services import (
    CustomerArchiveError,
    archive_customer,
    create_customer_idempotently,
    restore_customer,
    search_customers,
)

PAGE_SIZE = 50


def _return_to_new_customer_flow(request, customer):
    """Return safely to a local operator flow and keep the newly made card selected."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if not target or not url_has_allowed_host_and_scheme(
        target, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return None
    parsed = urlsplit(target)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["customer_id"] = str(customer.pk)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def _require_access(request) -> None:
    user = request.user
    if not (
        user.can_manage_sales
        or user.can_manage_repairs
        or user.can_manage_reservations
        or user.can_view_reports
    ):
        raise PermissionDenied


def _require_edit(request) -> None:
    user = request.user
    if not (user.can_manage_sales or user.can_manage_repairs or user.can_manage_reservations):
        raise PermissionDenied


@login_required
def customer_list(request):
    _require_access(request)
    query = (request.GET.get("q") or "").strip()
    archived = request.GET.get("archived") == "1"
    customers = search_customers(query, limit=1000, include_archived=archived)
    page_obj = Paginator(customers, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "customers/customer_list.html",
        {
            "page_obj": page_obj,
            "is_paginated": page_obj.paginator.num_pages > 1,
            "q": query,
            "show_archived": archived,
            "can_edit": (
                request.user.can_manage_sales
                or request.user.can_manage_repairs
                or request.user.can_manage_reservations
            ),
        },
    )


@login_required
def customer_create(request):
    _require_edit(request)
    if request.method == "POST":
        form = CustomerForm(request.POST)
        try:
            create_token = UUID(request.POST.get("client_create_token", ""))
        except (AttributeError, ValueError):
            # Direct legacy POSTs remain compatible; rendered forms always
            # carry a fresh UUID in the hidden field.
            create_token = uuid4()
        if form.is_valid():
            customer, _created = create_customer_idempotently(form, token=create_token)
            messages.success(request, f"Клиент {customer.name} создан.")
            target = _return_to_new_customer_flow(request, customer)
            if target:
                return redirect(target)
            return redirect("customer_detail", pk=customer.pk)
    else:
        form = CustomerForm(initial={"name": (request.GET.get("name") or "").strip()})
        create_token = uuid4()
    return render(
        request,
        "customers/customer_form.html",
        {
            "form": form,
            "title": "Новый клиент",
            "customer": None,
            "next": request.POST.get("next") or request.GET.get("next") or "",
            "client_create_token": str(create_token),
        },
    )


@login_required
def legacy_customer_link(request):
    """Завести карточку для исторической группы документов или выбрать готовую.

    Группа задана только своим историческим именем: список документов приходит
    не из браузера, а пересобирается на сервере при сохранении. Имя и телефон
    подсказываются из самой строки, но это подсказка - правит оператор.
    """
    _require_edit(request)
    legacy_name = (request.POST.get("legacy_name") or request.GET.get("legacy_name") or "").strip()
    if not legacy_name:
        messages.error(request, "Не указана историческая запись клиента.")
        return redirect("reports_clients_overview")
    summary = legacy_group_summary(legacy_name)
    if not summary["sales"] and not summary["repairs"]:
        messages.error(
            request, f"Документов без карточки с записью «{legacy_name}» не найдено."
        )
        return redirect("reports_clients_overview")
    suggestion = suggest_identity(legacy_name)
    back = request.POST.get("next") or request.GET.get("next") or ""

    if request.method == "POST":
        existing_id = (request.POST.get("existing_customer") or "").strip()
        if existing_id:
            customer = Customer.objects.filter(
                pk=existing_id, archived_at__isnull=True
            ).first()
            if customer is None:
                messages.error(request, "Выбранная карточка не найдена.")
                return redirect(request.get_full_path())
            try:
                result = link_legacy_group(
                    legacy_name=legacy_name, customer=customer, by=request.user
                )
            except ValueError as exc:
                messages.error(request, str(exc))
                return redirect(request.get_full_path())
            messages.success(
                request,
                f"Документы записи «{legacy_name}» привязаны к карточке {customer.name}: "
                f"продаж {result['sales_linked']}, ремонтов {result['repairs_linked']}.",
            )
            return redirect(back or "reports_clients_overview")
        form = CustomerForm(request.POST)
        if form.is_valid():
            customer = form.save()
            result = link_legacy_group(legacy_name=legacy_name, customer=customer, by=request.user)
            messages.success(
                request,
                f"Карточка {customer.name} создана, документы записи «{legacy_name}» "
                f"привязаны: продаж {result['sales_linked']}, "
                f"ремонтов {result['repairs_linked']}.",
            )
            return redirect(back or "reports_clients_overview")
    else:
        form = CustomerForm(initial={"name": suggestion["name"], "phone": suggestion["phone"]})

    query = (request.GET.get("q") or "").strip()
    return render(
        request,
        "customers/legacy_customer_link.html",
        {
            "form": form,
            "legacy_name": legacy_name,
            "summary": summary,
            "suggestion": suggestion,
            "next": back,
            "q": query,
            "candidates": search_customers(query)[:20] if query else [],
        },
    )

@login_required
def customer_edit(request, pk):
    _require_edit(request)
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == "POST":
        form = CustomerForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            messages.success(request, "Карточка клиента обновлена.")
            return redirect("customer_detail", pk=customer.pk)
    else:
        form = CustomerForm(instance=customer)
    return render(
        request,
        "customers/customer_form.html",
        {"form": form, "title": "Карточка клиента", "customer": customer},
    )


@login_required
def customer_detail(request, pk):
    _require_access(request)
    customer = get_object_or_404(Customer, pk=pk)
    sales = list(
        Sale.objects.filter(customer=customer)
        .select_related("sold_by")
        .order_by("-created_at")[:20]
    )
    repairs = list(
        RepairOrder.objects.filter(customer=customer)
        .select_related("created_by")
        .order_by("-created_at")[:20]
    )
    reservations = list(Reservation.objects.filter(customer=customer).order_by("-created_at")[:20])
    return render(
        request,
        "customers/customer_detail.html",
        {
            "customer": customer,
            "lifecycle_events": customer.lifecycle_events.select_related("actor")[:20],
            "sales": sales,
            "repairs": repairs,
            "reservations": reservations,
            "can_edit": (
                request.user.can_manage_sales
                or request.user.can_manage_repairs
                or request.user.can_manage_reservations
            ),
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
def customer_archive(request, pk):
    return _customer_lifecycle_transition(request, pk, restore=False)


@login_required
@require_http_methods(["GET", "POST"])
def customer_restore(request, pk):
    return _customer_lifecycle_transition(request, pk, restore=True)


def _customer_lifecycle_transition(request, pk, *, restore: bool):
    _require_edit(request)
    customer = get_object_or_404(Customer, pk=pk)
    action_label = "Восстановить" if restore else "Архивировать"
    is_currently_archived = customer.is_archived
    can_transition = is_currently_archived if restore else not is_currently_archived
    if request.method == "POST" and can_transition:
        if request.POST.get("confirm_transition") != "yes":
            messages.error(request, "Подтвердите действие.")
        else:
            try:
                transition = restore_customer if restore else archive_customer
                transition(
                    customer_id=customer.pk,
                    by=request.user,
                    reason=request.POST.get("reason", ""),
                )
            except CustomerArchiveError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(
                    request,
                    f"Карточка клиента «{customer.name}» "
                    f"{'восстановлена' if restore else 'перемещена в архив'}.",
                )
                return redirect("customer_detail", pk=customer.pk)
    return render(
        request,
        "customers/customer_lifecycle_confirm.html",
        {
            "customer": customer,
            "restore": restore,
            "action_label": action_label,
            "can_transition": can_transition,
        },
        status=200 if can_transition else 409,
    )
