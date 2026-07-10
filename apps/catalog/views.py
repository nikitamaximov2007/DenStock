from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import ListView, TemplateView

from . import forms
from .generic import DirectoryCreateView, DirectoryListView, DirectoryUpdateView, toggle_active
from .models import Category, Manufacturer, Unit, VehicleMake, VehicleModel, VehicleType
from .services import get_current_price_settings, update_current_price_settings


class DirectoryIndexView(LoginRequiredMixin, TemplateView):
    template_name = "directories/index.html"


@login_required
def price_settings(request):
    if not request.user.can_manage_parts:
        raise PermissionDenied

    settings = get_current_price_settings()
    initial = {
        "current_usd_rate": settings.current_usd_rate,
        "brp_markup_percent": settings.brp_markup_percent,
        "polaris_markup_percent": settings.polaris_markup_percent,
    }
    refreshed_count = None
    if request.method == "POST":
        form = forms.PriceSettingsForm(request.POST)
        if form.is_valid():
            settings, refreshed_count = update_current_price_settings(
                current_usd_rate=form.cleaned_data["current_usd_rate"],
                brp_markup_percent=form.cleaned_data["brp_markup_percent"],
                polaris_markup_percent=form.cleaned_data["polaris_markup_percent"],
                by=request.user,
            )
            messages.success(request, "Настройки цен сохранены")
            return redirect("price_settings")
    else:
        form = forms.PriceSettingsForm(initial=initial)

    return render(
        request,
        "directories/price_settings.html",
        {"form": form, "pricing": settings, "refreshed_count": refreshed_count},
    )


# --- Категории (дерево) ---
class CategoryListView(LoginRequiredMixin, ListView):
    template_name = "catalog/category_list.html"

    def get_queryset(self):
        qs = Category.objects.all()
        if self.request.GET.get("show", "active") != "all":
            qs = qs.filter(is_active=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        nodes = list(ctx["object_list"])
        children = {}
        for node in nodes:
            children.setdefault(node.parent_id, []).append(node)
        for items in children.values():
            items.sort(key=lambda c: (c.sort_order, c.name))

        rows = []

        def walk(parent_id, depth):
            for node in children.get(parent_id, []):
                rows.append({"obj": node, "depth": depth, "indent": depth * 24})
                walk(node.id, depth + 1)

        walk(None, 0)
        # Узлы, чей родитель отфильтрован (например, деактивирован), — как корни.
        seen = {r["obj"].id for r in rows}
        for node in nodes:
            if node.id not in seen:
                rows.append({"obj": node, "depth": 0, "indent": 0})

        ctx["rows"] = rows
        ctx["show"] = self.request.GET.get("show", "active")
        ctx["can_manage"] = self.request.user.can_manage_directories
        return ctx


class CategoryCreateView(DirectoryCreateView):
    model = Category
    form_class = forms.CategoryForm
    title = "Новая категория"
    success_url = reverse_lazy("category_list")


class CategoryUpdateView(DirectoryUpdateView):
    model = Category
    form_class = forms.CategoryForm
    title = "Редактирование категории"
    success_url = reverse_lazy("category_list")


@require_POST
def category_toggle(request, pk):
    return toggle_active(request, get_object_or_404(Category, pk=pk), "category_list")


# --- Производители ---
class ManufacturerListView(DirectoryListView):
    model = Manufacturer
    title = "Производители"
    headers = ["Название", "Страна"]
    create_url = "manufacturer_create"
    edit_url = "manufacturer_edit"
    toggle_url = "manufacturer_toggle"
    search_fields = ["name", "country"]

    def row_cells(self, obj):
        return [obj.name, obj.country]


class ManufacturerCreateView(DirectoryCreateView):
    model = Manufacturer
    form_class = forms.ManufacturerForm
    title = "Новый производитель"
    success_url = reverse_lazy("manufacturer_list")


class ManufacturerUpdateView(DirectoryUpdateView):
    model = Manufacturer
    form_class = forms.ManufacturerForm
    title = "Редактирование производителя"
    success_url = reverse_lazy("manufacturer_list")


@require_POST
def manufacturer_toggle(request, pk):
    return toggle_active(request, get_object_or_404(Manufacturer, pk=pk), "manufacturer_list")


# --- Единицы измерения ---
class UnitListView(DirectoryListView):
    model = Unit
    title = "Единицы измерения"
    headers = ["Название", "Сокращение"]
    create_url = "unit_create"
    edit_url = "unit_edit"
    toggle_url = "unit_toggle"
    search_fields = ["name", "short_name"]

    def row_cells(self, obj):
        return [obj.name, obj.short_name]


class UnitCreateView(DirectoryCreateView):
    model = Unit
    form_class = forms.UnitForm
    title = "Новая единица"
    success_url = reverse_lazy("unit_list")


class UnitUpdateView(DirectoryUpdateView):
    model = Unit
    form_class = forms.UnitForm
    title = "Редактирование единицы"
    success_url = reverse_lazy("unit_list")


@require_POST
def unit_toggle(request, pk):
    return toggle_active(request, get_object_or_404(Unit, pk=pk), "unit_list")


# --- Виды техники ---
class VehicleTypeListView(DirectoryListView):
    model = VehicleType
    title = "Виды техники"
    headers = ["Название"]
    create_url = "vehicletype_create"
    edit_url = "vehicletype_edit"
    toggle_url = "vehicletype_toggle"

    def row_cells(self, obj):
        return [obj.name]


class VehicleTypeCreateView(DirectoryCreateView):
    model = VehicleType
    form_class = forms.VehicleTypeForm
    title = "Новый вид техники"
    success_url = reverse_lazy("vehicletype_list")


class VehicleTypeUpdateView(DirectoryUpdateView):
    model = VehicleType
    form_class = forms.VehicleTypeForm
    title = "Редактирование вида техники"
    success_url = reverse_lazy("vehicletype_list")


@require_POST
def vehicletype_toggle(request, pk):
    return toggle_active(request, get_object_or_404(VehicleType, pk=pk), "vehicletype_list")


# --- Марки техники ---
class VehicleMakeListView(DirectoryListView):
    model = VehicleMake
    title = "Марки техники"
    headers = ["Марка", "Вид техники"]
    create_url = "vehiclemake_create"
    edit_url = "vehiclemake_edit"
    toggle_url = "vehiclemake_toggle"

    def get_queryset(self):
        return super().get_queryset().select_related("vehicle_type")

    def row_cells(self, obj):
        return [obj.name, str(obj.vehicle_type)]


class VehicleMakeCreateView(DirectoryCreateView):
    model = VehicleMake
    form_class = forms.VehicleMakeForm
    title = "Новая марка"
    success_url = reverse_lazy("vehiclemake_list")


class VehicleMakeUpdateView(DirectoryUpdateView):
    model = VehicleMake
    form_class = forms.VehicleMakeForm
    title = "Редактирование марки"
    success_url = reverse_lazy("vehiclemake_list")


@require_POST
def vehiclemake_toggle(request, pk):
    return toggle_active(request, get_object_or_404(VehicleMake, pk=pk), "vehiclemake_list")


# --- Модели техники ---
class VehicleModelListView(DirectoryListView):
    model = VehicleModel
    title = "Модели техники"
    headers = ["Модель", "Марка", "Годы"]
    create_url = "vehiclemodel_create"
    edit_url = "vehiclemodel_edit"
    toggle_url = "vehiclemodel_toggle"

    def get_queryset(self):
        return super().get_queryset().select_related("vehicle_make")

    def row_cells(self, obj):
        years = "—"
        if obj.year_from or obj.year_to:
            years = f"{obj.year_from or '…'}–{obj.year_to or '…'}"
        return [obj.name, str(obj.vehicle_make), years]


class VehicleModelCreateView(DirectoryCreateView):
    model = VehicleModel
    form_class = forms.VehicleModelForm
    title = "Новая модель"
    success_url = reverse_lazy("vehiclemodel_list")


class VehicleModelUpdateView(DirectoryUpdateView):
    model = VehicleModel
    form_class = forms.VehicleModelForm
    title = "Редактирование модели"
    success_url = reverse_lazy("vehiclemodel_list")


@require_POST
def vehiclemodel_toggle(request, pk):
    return toggle_active(request, get_object_or_404(VehicleModel, pk=pk), "vehiclemodel_list")
