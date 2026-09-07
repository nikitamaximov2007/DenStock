from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, render

from .models import CustomsOrder


@login_required
def customs_orders_list(request):
    if not request.user.can_view_purchase_cost:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    return render(request, "customs_orders/list.html", {"orders": CustomsOrder.objects.all()})


@login_required
def customs_order_detail(request, pk):
    if not request.user.can_view_purchase_cost:
        from django.core.exceptions import PermissionDenied
        raise PermissionDenied
    order = get_object_or_404(CustomsOrder.objects.prefetch_related("lines"), pk=pk)
    return render(request, "customs_orders/detail.html", {"order": order})
