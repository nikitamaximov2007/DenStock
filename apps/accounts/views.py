from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from .forms import UserCreateForm, UserEditForm
from .models import User
from .permissions import AdminRequiredMixin, admin_required


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "accounts/user_list.html"
    context_object_name = "users"
    ordering = ["username"]


class UserCreateView(AdminRequiredMixin, CreateView):
    model = User
    form_class = UserCreateForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")

    def form_valid(self, form):
        messages.success(self.request, "Пользователь создан.")
        return super().form_valid(form)


class UserUpdateView(AdminRequiredMixin, UpdateView):
    model = User
    form_class = UserEditForm
    template_name = "accounts/user_form.html"
    success_url = reverse_lazy("user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["editing_self"] = self.object == self.request.user
        return kwargs

    def form_valid(self, form):
        messages.success(self.request, "Изменения сохранены.")
        return super().form_valid(form)


@require_POST
@admin_required
def toggle_active(request, pk):
    target = get_object_or_404(User, pk=pk)
    # Защита от случайной самодеактивации.
    if target == request.user:
        messages.error(request, "Нельзя деактивировать собственную учётную запись.")
        return redirect("user_list")
    target.is_active = not target.is_active
    target.save(update_fields=["is_active"])
    state = "активирован" if target.is_active else "деактивирован"
    messages.success(request, f"Пользователь {target} {state}.")
    return redirect("user_list")


def permission_denied_view(request, exception=None):
    """Обработчик 403: понятная страница «Нет доступа»."""
    return render(request, "403.html", status=403)
