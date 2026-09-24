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
from django.views.decorators.http import require_POST

from apps.repairs.models import RepairOrder
from apps.sales.models import Reservation, Sale

from .dedup_audit import duplicate_group_for_phone
from .forms import CustomerForm
from .legacy_linking import legacy_group_summary, link_legacy_group, suggest_identity
from .merge import CustomerMergeError, execute_customer_merge, preview_customer_merge
from .models import Customer, CustomerCreateIdempotency
from .services import (
    check_duplicate_phone,
    create_customer_idempotently,
    documents_of,
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


def _require_merge(request) -> None:
    """Слияние карточек затрагивает продажи/ремонты/заявки по всей системе -
    более узкая роль, чем обычное редактирование карточки."""
    if not request.user.is_manager:
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
    """Создать карточку клиента.

    Совпадение канонического телефона с уже живой (не объединённой) карточкой
    никогда не создаёт дубликат молча:

    * ровно одно совпадение - показываем предупреждение с найденной карточкой
      и требуем явного подтверждения «Всё равно создать новую»;
    * несколько совпадений - создание запрещено, список для ручного разбора.
    """
    _require_edit(request)
    duplicate_check = None
    if request.method == "POST":
        form = CustomerForm(request.POST)
        try:
            create_token = UUID(request.POST.get("client_create_token", ""))
        except (AttributeError, ValueError):
            # Direct legacy POSTs remain compatible; rendered forms always
            # carry a fresh UUID in the hidden field.
            create_token = uuid4()
        if form.is_valid():
            # Повтор ОДНОГО и того же запроса (тот же токен, уже завершённый
            # ранее) - не новая карточка, а идемпотентный возврат существующей:
            # проверка дубликата здесь неуместна и заблокировала бы легитимный
            # повторный клик/ретрай сети.
            is_replay = CustomerCreateIdempotency.objects.filter(
                token=create_token, customer__isnull=False
            ).exists()
            phone = form.cleaned_data.get("phone") or ""
            duplicate_check = None if is_replay else check_duplicate_phone(phone)
            confirmed = request.POST.get("confirm_duplicate") == "1"
            if duplicate_check is not None and duplicate_check.is_multiple:
                messages.error(
                    request,
                    "По этому телефону уже есть несколько карточек - создание новой "
                    "запрещено. Выберите подходящую вручную.",
                )
            elif duplicate_check is not None and duplicate_check.is_single and not confirmed:
                pass  # falls through to render the warning below
            else:
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
            "duplicate_check": duplicate_check,
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
            customer = Customer.objects.filter(pk=existing_id).first()
            if customer is None:
                messages.error(request, "Выбранная карточка не найдена.")
                return redirect(request.get_full_path())
            result = link_legacy_group(legacy_name=legacy_name, customer=customer, by=request.user)
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
    duplicate_group = None
    if not customer.is_merged and customer.phone_normalized:
        duplicate_group = duplicate_group_for_phone(
            customer.phone_normalized, exclude_id=customer.pk
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
            "can_merge": request.user.is_manager,
            "duplicate_group": duplicate_group,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@login_required
def customer_compare(request, pk, other_pk):
    """Сравнить две карточки и выбрать, какая станет канонической.

    Ничего не меняет: обе половины формы ведут на подтверждение
    (customer_merge_confirm), а не сразу на объединение.
    """
    _require_merge(request)
    if pk == other_pk:
        messages.error(request, "Нельзя сравнивать карточку саму с собой.")
        return redirect("customer_detail", pk=pk)
    first = get_object_or_404(Customer, pk=pk)
    second = get_object_or_404(Customer, pk=other_pk)
    return render(
        request,
        "customers/customer_compare.html",
        {
            "first": first,
            "second": second,
            "first_documents": documents_of(first),
            "second_documents": documents_of(second),
        },
    )


@login_required
def customer_merge_confirm(request, pk, other_pk):
    """Явное подтверждение: что именно перенесётся, прежде чем что-то менять."""
    _require_merge(request)
    target_id = int(request.GET.get("target", pk))
    source_id = other_pk if target_id == pk else pk
    if target_id not in (pk, other_pk):
        messages.error(request, "Некорректный выбор канонической карточки.")
        return redirect("customer_compare", pk=pk, other_pk=other_pk)
    target = get_object_or_404(Customer, pk=target_id)
    source = get_object_or_404(Customer, pk=source_id)
    try:
        plan = preview_customer_merge(target, source)
    except CustomerMergeError as exc:
        messages.error(request, str(exc))
        return redirect("customer_compare", pk=pk, other_pk=other_pk)
    return render(
        request,
        "customers/customer_merge_confirm.html",
        {"target": target, "source": source, "plan": plan},
    )


@login_required
@require_POST
def customer_merge_apply(request):
    """Выполнить объединение. target/source передаются явно в теле запроса -
    ID из URL здесь не участвуют, чтобы выбор канонической карточки не мог
    перепутаться между экранами."""
    _require_merge(request)
    try:
        target_id = int(request.POST.get("target_id", ""))
        source_id = int(request.POST.get("source_id", ""))
    except (TypeError, ValueError):
        messages.error(request, "Некорректные параметры объединения.")
        return redirect("customer_list")
    reason = (request.POST.get("reason") or "").strip()
    try:
        receipt = execute_customer_merge(
            target_id=target_id, source_id=source_id, by=request.user, reason=reason
        )
    except CustomerMergeError as exc:
        messages.error(request, str(exc))
        return redirect("customer_compare", pk=target_id, other_pk=source_id)
    total = sum(receipt.moved_counts.values())
    messages.success(
        request,
        f"Карточка #{source_id} объединена с #{target_id}: перенесено связей - {total}.",
    )
    return redirect("customer_detail", pk=target_id)
