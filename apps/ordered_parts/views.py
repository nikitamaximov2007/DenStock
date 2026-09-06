"""Раздел «Запчасти на заказ». View - оркестратор, правила в services.

Права те же, что у продаж: заказ детали клиенту это продажная работа, и
отдельной модели доступа для неё заводить незачем. Аноним не проходит вовсе.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from apps.customers.models import Customer
from apps.customers.services import search_customers

from .models import OrderedPart
from .services import (
    OrderedPartError,
    create_ordered_part,
    ordered_parts_list,
    resolve_ordered_article,
    resolve_ordered_part_by_id,
    update_ordered_part,
)

PAGE_SIZE = 50


def _require_access(request) -> None:
    if not request.user.can_manage_sales:
        raise PermissionDenied


def _customer_choices(query: str):
    """Тот же справочник и тот же поиск, что у продаж и ремонтов."""
    return search_customers(query, limit=200) if query else Customer.objects.all()[:200]


@login_required
def ordered_part_list(request):
    _require_access(request)
    page_obj = Paginator(ordered_parts_list(), PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "ordered_parts/list.html",
        {"page_obj": page_obj, "total": OrderedPart.objects.count()},
    )


@login_required
def ordered_part_create(request):
    _require_access(request)
    article = (request.POST.get("article") or request.GET.get("article") or "").strip()
    customer_query = (request.POST.get("customer_q") or request.GET.get("customer_q") or "").strip()
    prepayment = request.POST.get("prepayment") or request.GET.get("prepayment") or ""
    customer_id = request.POST.get("customer_id") or request.GET.get("customer_id") or ""
    part_id = request.POST.get("part_id") or request.GET.get("part_id") or ""
    candidate, options, error = None, [], ""

    if article or part_id:
        try:
            if part_id:
                candidate = resolve_ordered_part_by_id(part_id)
                article = candidate.exact_number or article
            else:
                candidate, _result = resolve_ordered_article(article)
        except OrderedPartError as exc:
            error = str(exc)
            # Неоднозначный артикул не выбирается за оператора: показываем, из
            # чего выбирать, и ждём его решения.
            from apps.core.part_lookup import resolve_part_lookup

            lookup = resolve_part_lookup(article, include_price=True)
            if lookup.status in {"ambiguous", "multiple"}:
                options = lookup.candidates

    if request.method == "POST" and request.POST.get("action") == "create":
        customer = Customer.objects.filter(pk=customer_id).first() if customer_id else None
        try:
            if candidate is None:
                raise OrderedPartError(error or "Укажите артикул запчасти.")
            order = create_ordered_part(
                candidate=candidate, customer=customer,
                prepayment=prepayment, by=request.user,
            )
        except OrderedPartError as exc:
            error = str(exc)
        else:
            messages.success(
                request, f"Заказ на {order.article} для клиента {order.customer.name} оформлен."
            )
            return redirect("ordered_part_list")

    return render(
        request,
        "ordered_parts/form.html",
        {
            "article": article,
            "candidate": candidate,
            "options": options,
            "error": error,
            "customers": _customer_choices(customer_query),
            "customer_q": customer_query,
            "customer_id": customer_id,
            "prepayment": prepayment,
            "title": "Новая запчасть на заказ",
        },
    )


@login_required
def ordered_part_edit(request, pk):
    """Правка ошибки оператора: клиент и предоплата. Деталь не подменяется."""
    _require_access(request)
    order = get_object_or_404(
        OrderedPart.objects.select_related("customer", "part_type"), pk=pk
    )
    customer_query = (request.POST.get("customer_q") or request.GET.get("customer_q") or "").strip()
    error = ""
    if request.method == "POST":
        customer = Customer.objects.filter(pk=request.POST.get("customer_id") or "").first()
        try:
            update_ordered_part(
                order, customer=customer,
                prepayment=request.POST.get("prepayment"), by=request.user,
            )
        except OrderedPartError as exc:
            error = str(exc)
        else:
            messages.success(request, f"Заказ на {order.article} обновлён.")
            return redirect("ordered_part_list")
    return render(
        request,
        "ordered_parts/edit.html",
        {
            "order": order,
            "error": error,
            "customers": _customer_choices(customer_query),
            "customer_q": customer_query,
            "title": f"Заказ на {order.article}",
        },
    )
