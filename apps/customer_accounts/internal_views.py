"""Small staff-only ownership-link screen for messenger cabinet accounts."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.customers.models import Customer

from . import services
from .models import CustomerAccount, CustomerAccountCustomerLink


def _staff_only(request):
    if not (request.user.is_admin or request.user.is_manager):
        raise Http404


@login_required
@require_http_methods(["GET", "POST"])
def ownership_links(request):
    _staff_only(request)
    if request.method == "POST":
        account = CustomerAccount.objects.filter(pk=request.POST.get("account_id")).first()
        if account is None:
            messages.error(request, "Кабинет не найден.")
        elif request.POST.get("action") == "unlink":
            services.unlink_customer(account, by_user=request.user)
            messages.success(request, "Связь кабинета снята.")
        else:
            customer = Customer.objects.filter(pk=request.POST.get("customer_id")).first()
            if customer is None:
                messages.error(request, "Карточка клиента не найдена.")
            else:
                try:
                    services.link_customer(account, customer, by_user=request.user)
                except services.AccountError as exc:
                    messages.error(request, str(exc))
                else:
                    messages.success(request, "Кабинет привязан к карточке клиента.")
        return redirect("customer_account_internal_links")
    query = " ".join(str(request.GET.get("q", "")).split())[:80]
    account_qs = CustomerAccount.objects.prefetch_related("identities").order_by("pk")
    customer_qs = Customer.objects.order_by("name", "pk")
    if query:
        account_qs = account_qs.filter(
            Q(display_name__icontains=query) | Q(identities__display_name__icontains=query)
        ).distinct()
        customer_qs = customer_qs.filter(
            Q(name__icontains=query) | Q(phone__icontains=query)
        )
    account_page = Paginator(account_qs, 25).get_page(request.GET.get("accounts_page"))
    customer_page = Paginator(customer_qs, 50).get_page(request.GET.get("customers_page"))
    page_account_ids = [account.pk for account in account_page.object_list]
    active_links = {
        link.account_id: link
        for link in CustomerAccountCustomerLink.objects.select_related("customer")
        .filter(unlinked_at__isnull=True, account_id__in=page_account_ids)
    }
    rows = []
    for account in account_page.object_list:
        rows.append({"account": account, "link": active_links.get(account.pk)})
    return render(request, "customer_accounts/internal_links.html", {
        "rows": rows, "account_page": account_page, "customer_page": customer_page,
        "query": query,
    })
