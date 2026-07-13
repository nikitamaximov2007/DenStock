from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.permissions import ManagePartsMixin
from apps.core.forms import ImageUploadForm
from apps.core.images import add_image, deactivate_image, set_primary
from apps.inventory.presentation import attach_part_identity, with_part_identity

from .forms import PartBarcodeForm, PartCompatibilityForm, PartNumberForm, PartTypeForm
from .models import (
    PartBarcode,
    PartCompatibility,
    PartNumber,
    PartType,
    PartTypeImage,
    normalize_number,
)


class PartTypeListView(LoginRequiredMixin, ListView):
    template_name = "catalog/part_list.html"
    paginate_by = 50

    def get_queryset(self):
        # with_part_identity добавляет select_related BRP/Polaris-связей и
        # prefetch допустимых номеров — точный артикул строки считается без
        # N+1 канонической логикой part_exact_number (см. presentation.py).
        qs = with_part_identity(
            PartType.objects.select_related("category", "unit"), part_field=""
        )
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(
                Q(name__icontains=query)
                | Q(numbers__normalized_value__icontains=normalize_number(query))
            ).distinct()
        if self.request.GET.get("show", "active") != "all":
            qs = qs.filter(is_active=True)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        # Точный артикул готовится во view (не в шаблоне) по канонической
        # логике: BRP material_no -> Polaris part_number -> primary OEM/ARTICLE
        # -> любой OEM/ARTICLE -> пусто. Аналог/INTERNAL_REF/лот сюда не попадают.
        attach_part_identity(ctx["object_list"], part_attr="")
        ctx["q"] = self.request.GET.get("q", "")
        ctx["show"] = self.request.GET.get("show", "active")
        ctx["can_manage"] = self.request.user.can_manage_parts
        return ctx


class PartTypeDetailView(LoginRequiredMixin, DetailView):
    model = PartType
    template_name = "catalog/part_detail.html"
    context_object_name = "part"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["can_manage"] = self.request.user.can_manage_parts
        ctx["can_print_labels"] = self.request.user.can_print_labels
        ctx["can_manage_images"] = self.request.user.can_manage_images
        ctx["numbers"] = self.object.numbers.all()
        ctx["barcodes"] = self.object.barcodes.all()
        ctx["compatibilities"] = self.object.compatibilities.select_related("vehicle_model")
        images = self.object.images.filter(is_active=True)
        ctx["images"] = images
        ctx["primary_image"] = next((i for i in images if i.is_primary), None)
        if ctx["can_manage"]:
            ctx["number_form"] = PartNumberForm()
            ctx["barcode_form"] = PartBarcodeForm()
            ctx["compat_form"] = PartCompatibilityForm()
        return ctx


class PartTypeCreateView(ManagePartsMixin, CreateView):
    model = PartType
    form_class = PartTypeForm
    template_name = "directories/form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Новая деталь"
        return ctx

    def get_success_url(self):
        messages.success(self.request, "Деталь создана.")
        # Layer 28: возврат в вызвавший экран (например, черновик поступления)
        # с pk созданной детали. Только безопасные relative-URL.
        nxt = self.request.GET.get("next") or ""
        if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts=None):
            sep = "&" if "?" in nxt else "?"
            return f"{nxt}{sep}new_part={self.object.pk}"
        return reverse("part_detail", args=[self.object.pk])


class PartTypeUpdateView(ManagePartsMixin, UpdateView):
    model = PartType
    form_class = PartTypeForm
    template_name = "directories/form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Редактирование детали"
        return ctx

    def get_success_url(self):
        messages.success(self.request, "Изменения сохранены.")
        return reverse("part_detail", args=[self.object.pk])


def _require_parts(request) -> None:
    if not request.user.can_manage_parts:
        raise PermissionDenied


@require_POST
def part_toggle(request, pk):
    _require_parts(request)
    part = get_object_or_404(PartType, pk=pk)
    part.is_active = not part.is_active
    part.save(update_fields=["is_active", "updated_at"])
    state = "активирована" if part.is_active else "деактивирована"
    messages.success(request, f"Деталь {state}: {part}")
    return redirect("part_detail", pk=pk)


def _add_subrecord(request, part, form_class):
    form = form_class(request.POST)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.part = part
        obj.save()
        messages.success(request, "Добавлено.")
    else:
        messages.error(request, "Не удалось добавить: проверьте значение (возможно, дубликат).")
    return redirect("part_detail", pk=part.pk)


@require_POST
def number_add(request, pk):
    _require_parts(request)
    return _add_subrecord(request, get_object_or_404(PartType, pk=pk), PartNumberForm)


@require_POST
def barcode_add(request, pk):
    _require_parts(request)
    return _add_subrecord(request, get_object_or_404(PartType, pk=pk), PartBarcodeForm)


@require_POST
def compat_add(request, pk):
    _require_parts(request)
    return _add_subrecord(request, get_object_or_404(PartType, pk=pk), PartCompatibilityForm)


@require_POST
def number_delete(request, pk):
    _require_parts(request)
    number = get_object_or_404(PartNumber, pk=pk)
    part_pk = number.part_id
    number.delete()
    messages.success(request, "Номер удалён.")
    return redirect("part_detail", pk=part_pk)


@require_POST
def barcode_delete(request, pk):
    _require_parts(request)
    barcode = get_object_or_404(PartBarcode, pk=pk)
    part_pk = barcode.part_id
    barcode.delete()
    messages.success(request, "Штрихкод удалён.")
    return redirect("part_detail", pk=part_pk)


@require_POST
def compat_delete(request, pk):
    _require_parts(request)
    compat = get_object_or_404(PartCompatibility, pk=pk)
    part_pk = compat.part_id
    compat.delete()
    messages.success(request, "Совместимость удалена.")
    return redirect("part_detail", pk=part_pk)


# --- Слой 24: фотографии вида детали (информационный слой, без складской физики) ---


def _require_images(request) -> None:
    if not request.user.can_manage_images:
        raise PermissionDenied


@login_required
@require_POST
def part_image_add(request, pk):
    _require_images(request)
    part = get_object_or_404(PartType, pk=pk)
    form = ImageUploadForm(request.POST, request.FILES)
    if form.is_valid():
        add_image(
            part.images, image=form.cleaned_data["image"],
            caption=form.cleaned_data["caption"], by=request.user,
        )
        messages.success(request, "Фото добавлено.")
    else:
        messages.error(request, "; ".join(form.errors.get("image", ["Не удалось загрузить фото."])))
    return redirect("part_detail", pk=pk)


@login_required
@require_POST
def part_image_primary(request, pk):
    _require_images(request)
    image = get_object_or_404(PartTypeImage, pk=pk)
    set_primary(image)
    messages.success(request, "Главное фото обновлено.")
    return redirect("part_detail", pk=image.part_id)


@login_required
@require_POST
def part_image_delete(request, pk):
    _require_images(request)
    image = get_object_or_404(PartTypeImage, pk=pk)
    part_pk = image.part_id
    deactivate_image(image)
    messages.success(request, "Фото удалено.")
    return redirect("part_detail", pk=part_pk)
