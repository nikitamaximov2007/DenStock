from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView, UpdateView

from apps.accounts.permissions import ManageBatchesMixin

from .forms import BatchForm, BatchLineForm
from .models import Batch, BatchLine
from .services import LandedCostError, compute_landed_cost, finalize_cost


def _can_see_batches(user) -> bool:
    return user.can_manage_batches or user.can_view_purchase_cost or user.is_storekeeper


class BatchAccessMixin:
    """Просмотр партий — ролям снабжения/склада. Продавцу/Мастеру — нет."""

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not _can_see_batches(request.user):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class BatchListView(BatchAccessMixin, ListView):
    model = Batch
    template_name = "procurement/batch_list.html"
    context_object_name = "batches"
    paginate_by = 50

    def get_queryset(self):
        qs = Batch.objects.select_related("supplier")
        status = self.request.GET.get("status")
        if status:
            qs = qs.filter(status=status)
        query = self.request.GET.get("q", "").strip()
        if query:
            qs = qs.filter(number__icontains=query)
        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_batches
        ctx["statuses"] = Batch.Status.choices
        ctx["status"] = self.request.GET.get("status", "")
        ctx["q"] = self.request.GET.get("q", "")
        return ctx


class BatchDetailView(BatchAccessMixin, DetailView):
    model = Batch
    template_name = "procurement/batch_detail.html"
    context_object_name = "batch"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["show_costs"] = self.request.user.can_view_purchase_cost
        ctx["can_manage"] = self.request.user.can_manage_batches
        ctx["lines"] = self.object.lines.select_related("part_type")
        allowed = self.object.ALLOWED_TRANSITIONS.get(self.object.status, [])
        ctx["next_statuses"] = [(s, Batch.Status(s).label) for s in allowed]
        return ctx


class BatchCreateView(ManageBatchesMixin, CreateView):
    model = Batch
    form_class = BatchForm
    template_name = "procurement/batch_form.html"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Новая партия"
        return ctx

    def form_valid(self, form):
        form.instance.created_by = self.request.user
        messages.success(self.request, "Партия создана.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("batch_detail", args=[self.object.pk])


class BatchUpdateView(ManageBatchesMixin, UpdateView):
    model = Batch
    form_class = BatchForm
    template_name = "procurement/batch_form.html"

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.costs_editable:
            messages.error(request, "Себестоимость зафиксирована — партию изменять нельзя.")
            return redirect("batch_detail", pk=self.object.pk)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Редактирование партии"
        return ctx

    def get_success_url(self):
        messages.success(self.request, "Изменения сохранены.")
        return reverse("batch_detail", args=[self.object.pk])


@require_POST
def batch_status_change(request, pk):
    if not request.user.can_manage_batches:
        raise PermissionDenied
    batch = get_object_or_404(Batch, pk=pk)
    new_status = request.POST.get("status", "")
    if batch.can_transition_to(new_status):
        batch.status = new_status
        batch.save(update_fields=["status", "updated_at"])
        messages.success(request, f"Статус партии: {batch.get_status_display()}.")
    else:
        messages.error(request, "Недопустимый переход статуса.")
    return redirect("batch_detail", pk=pk)


def cost_preview(request, pk):
    """Предпросмотр распределения landed cost без сохранения (защита от случайной фиксации)."""
    if not request.user.can_manage_batches:
        raise PermissionDenied
    batch = get_object_or_404(Batch, pk=pk)
    if batch.status != Batch.Status.ACCEPTED:
        messages.error(request, "Рассчитать себестоимость можно только для принятой партии.")
        return redirect("batch_detail", pk=pk)
    try:
        computed = compute_landed_cost(batch)
    except LandedCostError as exc:
        messages.error(request, str(exc))
        return redirect("batch_detail", pk=pk)
    base_total = sum((row["line"].total_cost_rub for row in computed["lines"]), Decimal("0"))
    grand_total = base_total + computed["extra"]
    return render(
        request,
        "procurement/cost_preview.html",
        {
            "batch": batch,
            "computed": computed,
            "method_label": Batch.AllocationMethod(computed["method"]).label,
            "base_total": base_total,
            "grand_total": grand_total,
            "show_costs": request.user.can_view_purchase_cost,
        },
    )


@require_POST
def cost_finalize(request, pk):
    if not request.user.can_manage_batches:
        raise PermissionDenied
    batch = get_object_or_404(Batch, pk=pk)
    try:
        finalize_cost(batch, request.user)
    except LandedCostError as exc:
        messages.error(request, str(exc))
        return redirect("batch_detail", pk=pk)
    messages.success(request, "Себестоимость рассчитана и зафиксирована.")
    return redirect("batch_detail", pk=pk)


class BatchLineCreateView(ManageBatchesMixin, CreateView):
    model = BatchLine
    form_class = BatchLineForm
    template_name = "procurement/line_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.batch = get_object_or_404(Batch, pk=kwargs["pk"])
        return super().dispatch(request, *args, **kwargs)

    def post(self, request, *args, **kwargs):
        if not self.batch.lines_editable:
            messages.error(request, "Партия закрыта для изменения строк.")
            return redirect("batch_detail", pk=self.batch.pk)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = f"Строка партии {self.batch.number}"
        ctx["batch"] = self.batch
        return ctx

    def form_valid(self, form):
        form.instance.batch = self.batch
        messages.success(self.request, "Строка добавлена.")
        return super().form_valid(form)

    def get_success_url(self):
        return reverse("batch_detail", args=[self.batch.pk])


class BatchLineUpdateView(ManageBatchesMixin, UpdateView):
    model = BatchLine
    form_class = BatchLineForm
    template_name = "procurement/line_form.html"

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self.object.batch.lines_editable:
            messages.error(request, "Партия закрыта для изменения строк.")
            return redirect("batch_detail", pk=self.object.batch.pk)
        return super().post(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = "Редактирование строки"
        ctx["batch"] = self.object.batch
        return ctx

    def get_success_url(self):
        messages.success(self.request, "Строка сохранена.")
        return reverse("batch_detail", args=[self.object.batch.pk])


@require_POST
def line_delete(request, pk):
    if not request.user.can_manage_batches:
        raise PermissionDenied
    line = get_object_or_404(BatchLine, pk=pk)
    batch_pk = line.batch_id
    if line.batch.status != Batch.Status.DRAFT:
        messages.error(request, "Удалять строки можно только в статусе «Создана».")
        return redirect("batch_detail", pk=batch_pk)
    line.delete()
    messages.success(request, "Строка удалена.")
    return redirect("batch_detail", pk=batch_pk)
