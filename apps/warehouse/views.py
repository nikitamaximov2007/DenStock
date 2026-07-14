from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse, reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, FormView, ListView, UpdateView

from apps.accounts.permissions import ManageWarehouseMixin

from .forms import StorageLocationForm, StorageLocationRenameForm, StorageLocationUpdateForm
from .models import StorageLocation
from .services import StorageLocationRenameError, rename_storage_location


def _safe_internal_next(request, candidate: str) -> str:
    """Accept only an application-relative return URL."""
    if (
        candidate
        and candidate.startswith("/")
        and not candidate.startswith("//")
        and url_has_allowed_host_and_scheme(
            candidate,
            allowed_hosts={request.get_host()},
            require_https=request.is_secure(),
        )
    ):
        return candidate
    return ""


class LocationTreeView(LoginRequiredMixin, ListView):
    """Цифровая карта склада — дерево мест с отступами."""

    template_name = "warehouse/location_tree.html"

    def get_queryset(self):
        qs = StorageLocation.objects.all()
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
            items.sort(key=lambda n: (n.sort_order, n.code))

        rows = []

        def walk(parent_id, depth):
            for node in children.get(parent_id, []):
                rows.append({"obj": node, "depth": depth, "indent": depth * 24})
                walk(node.id, depth + 1)

        walk(None, 0)
        seen = {r["obj"].id for r in rows}
        for node in nodes:
            if node.id not in seen:
                rows.append({"obj": node, "depth": 0, "indent": 0})

        ctx["rows"] = rows
        ctx["show"] = self.request.GET.get("show", "active")
        ctx["can_manage"] = self.request.user.can_manage_warehouse
        return ctx


class LocationDetailView(LoginRequiredMixin, DetailView):
    model = StorageLocation
    template_name = "warehouse/location_detail.html"
    context_object_name = "location"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_manage"] = self.request.user.can_manage_warehouse
        ctx["can_print_labels"] = self.request.user.can_print_labels
        ctx["children"] = self.object.children.all()
        ctx["rename_history"] = self.object.rename_history.select_related("renamed_by")[:20]
        return ctx


class LocationCreateView(ManageWarehouseMixin, CreateView):
    model = StorageLocation
    form_class = StorageLocationForm
    template_name = "directories/form.html"
    success_url = reverse_lazy("warehouse_index")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Новое место хранения"
        return ctx

    def form_valid(self, form):
        messages.success(self.request, "Место хранения создано.")
        return super().form_valid(form)


class LocationUpdateView(ManageWarehouseMixin, UpdateView):
    model = StorageLocation
    form_class = StorageLocationUpdateForm
    template_name = "warehouse/location_edit.html"
    success_url = reverse_lazy("warehouse_index")

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Редактирование места хранения"
        return ctx

    def form_valid(self, form):
        messages.success(self.request, "Изменения сохранены.")
        return super().form_valid(form)


class LocationRenameView(ManageWarehouseMixin, FormView):
    form_class = StorageLocationRenameForm
    template_name = "warehouse/location_rename.html"

    def dispatch(self, request, *args, **kwargs):
        self.location = get_object_or_404(StorageLocation, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def get_initial(self):
        initial = super().get_initial()
        initial["expected_code"] = self.location.code
        initial["next"] = _safe_internal_next(
            self.request,
            self.request.GET.get("next", ""),
        )
        return initial

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["location"] = self.location
        candidate = self.request.POST.get("next", "") if self.request.method == "POST" else ""
        ctx["cancel_url"] = _safe_internal_next(self.request, candidate) or _safe_internal_next(
            self.request,
            self.request.GET.get("next", ""),
        ) or reverse("location_detail", args=[self.location.pk])
        return ctx

    def form_valid(self, form):
        old_code = self.location.code
        try:
            location = rename_storage_location(
                self.location,
                new_code=form.cleaned_data["new_code"],
                expected_code=form.cleaned_data["expected_code"],
                by=self.request.user,
            )
        except StorageLocationRenameError as exc:
            form.add_error("new_code", str(exc))
            return self.form_invalid(form)

        messages.success(self.request, f"Ячейка {old_code} переименована в {location.code}.")
        messages.warning(self.request, "Распечатайте новую этикетку для ячейки.")
        return redirect(
            _safe_internal_next(self.request, form.cleaned_data.get("next", ""))
            or reverse("location_detail", args=[location.pk])
        )


@require_POST
def location_toggle(request, pk):
    if not request.user.can_manage_warehouse:
        raise PermissionDenied
    loc = get_object_or_404(StorageLocation, pk=pk)
    if loc.is_active:
        if loc.children.filter(is_active=True).exists():
            messages.error(
                request,
                "Нельзя деактивировать место с активными вложенными местами.",
            )
            return redirect("warehouse_index")
        loc.is_active = False
    else:
        if loc.parent and not loc.parent.is_active:
            messages.error(request, "Нельзя активировать место, родитель которого неактивен.")
            return redirect("warehouse_index")
        loc.is_active = True
    loc.save(update_fields=["is_active", "updated_at"])
    state = "активировано" if loc.is_active else "деактивировано"
    messages.success(request, f"Место {state}: {loc}")
    return redirect("warehouse_index")
