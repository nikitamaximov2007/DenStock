"""Small staff-only ownership-link screen for messenger cabinet accounts."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
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
    customers = list(Customer.objects.order_by("name", "pk"))
    active_links = {
        link.account_id: link
        for link in CustomerAccountCustomerLink.objects.select_related("customer")
        .filter(unlinked_at__isnull=True)
    }
    rows = []
    for account in CustomerAccount.objects.prefetch_related("identities").all():
        rows.append({"account": account, "link": active_links.get(account.pk)})
    return render(request, "customer_accounts/internal_links.html", {
        "rows": rows, "customers": customers,
    })
