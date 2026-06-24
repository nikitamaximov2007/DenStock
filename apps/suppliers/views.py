from django.shortcuts import get_object_or_404
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST

from apps.catalog.generic import (
    DirectoryCreateView,
    DirectoryListView,
    DirectoryUpdateView,
    toggle_active,
)

from .forms import SupplierForm
from .models import Supplier


class SupplierListView(DirectoryListView):
    model = Supplier
    title = "Поставщики"
    headers = ["Название", "Страна", "Валюта"]
    create_url = "supplier_create"
    edit_url = "supplier_edit"
    toggle_url = "supplier_toggle"
    search_fields = ["name", "country", "contact_person"]

    def row_cells(self, obj):
        return [obj.name, obj.country, obj.default_currency]


class SupplierCreateView(DirectoryCreateView):
    model = Supplier
    form_class = SupplierForm
    title = "Новый поставщик"
    success_url = reverse_lazy("supplier_list")


class SupplierUpdateView(DirectoryUpdateView):
    model = Supplier
    form_class = SupplierForm
    title = "Редактирование поставщика"
    success_url = reverse_lazy("supplier_list")


@require_POST
def supplier_toggle(request, pk):
    return toggle_active(request, get_object_or_404(Supplier, pk=pk), "supplier_list")
