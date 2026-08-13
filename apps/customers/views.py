"""Справочник клиентов. View - оркестратор, бизнес-логика в services.

Доступ повторяет существующую модель прав: карточка клиента нужна тем, кто
оформляет продажи, резервы или ремонты. Отдельного ACL не заводим.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render

from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

from .forms import CustomerForm
from .models import Customer
from .services import search_customers

PAGE_SIZE = 50


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
    customers = search_customers(query, limit=1000) if query else Customer.objects.all()
    page_obj = Paginator(customers, PAGE_SIZE).get_page(request.GET.get("page"))
    return render(
        request,
        "customers/customer_list.html",
        {
            "page_obj": page_obj,
            "is_paginated": page_obj.paginator.num_pages > 1,
            "q": query,
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
        if form.is_valid():
            customer = form.save()
            messages.success(request, f"Клиент {customer.name} создан.")
            return redirect("customer_detail", pk=customer.pk)
    else:
        form = CustomerForm(initial={"name": (request.GET.get("name") or "").strip()})
    return render(
        request,
        "customers/customer_form.html",
        {"form": form, "title": "Новый клиент", "customer": None},
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
        Sale.objects.filter(customer=customer).select_related("sold_by").order_by("-created_at")[:20]
    )
    repairs = list(
        RepairOrder.objects.filter(customer=customer)
        .select_related("created_by")
        .order_by("-created_at")[:20]
    )
    reservations = list(
        Reservation.objects.filter(customer=customer).order_by("-created_at")[:20]
    )
    return render(
        request,
        "customers/customer_detail.html",
        {
            "customer": customer,
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
