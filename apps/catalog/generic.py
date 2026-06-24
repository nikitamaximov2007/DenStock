"""Общие представления справочников: список (поиск + фильтр активности),
создание, редактирование, переключение активности. Переиспользуются catalog и
suppliers, чтобы не дублировать CRUD."""
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db.models import Q
from django.shortcuts import redirect
from django.views.generic import CreateView, ListView, UpdateView

from apps.accounts import roles
from apps.accounts.permissions import ManageDirectoriesMixin


class DirectoryListView(LoginRequiredMixin, ListView):
    """Список справочника. Просмотр доступен любому авторизованному."""

    template_name = "directories/list.html"
    search_fields = ["name"]
    title = "Справочник"
    headers: list[str] = ["Название"]
    create_url = ""
    edit_url = ""
    toggle_url = ""

    def get_queryset(self):
        qs = super().get_queryset()
        query = self.request.GET.get("q", "").strip()
        if query:
            cond = Q()
            for field in self.search_fields:
                cond |= Q(**{f"{field}__icontains": query})
            qs = qs.filter(cond)
        if self.request.GET.get("show", "active") != "all":
            qs = qs.filter(is_active=True)
        return qs

    def row_cells(self, obj) -> list:
        return [obj.name]

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["rows"] = [{"obj": o, "cells": self.row_cells(o)} for o in ctx["object_list"]]
        ctx["title"] = self.title
        ctx["headers"] = self.headers
        ctx["create_url"] = self.create_url
        ctx["edit_url"] = self.edit_url
        ctx["toggle_url"] = self.toggle_url
        ctx["q"] = self.request.GET.get("q", "")
        ctx["show"] = self.request.GET.get("show", "active")
        ctx["can_manage"] = self.request.user.can_manage_directories
        return ctx


class DirectoryCreateView(ManageDirectoriesMixin, CreateView):
    template_name = "directories/form.html"
    title = "Новая запись"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = self.title
        return ctx

    def form_valid(self, form):
        messages.success(self.request, "Запись создана.")
        return super().form_valid(form)


class DirectoryUpdateView(ManageDirectoriesMixin, UpdateView):
    template_name = "directories/form.html"
    title = "Редактирование"

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["title"] = self.title
        return ctx

    def form_valid(self, form):
        messages.success(self.request, "Изменения сохранены.")
        return super().form_valid(form)


def toggle_active(request, obj, redirect_to: str):
    """Активация/деактивация вместо удаления. Требует MANAGE_DIRECTORIES."""
    if not request.user.has_capability(roles.MANAGE_DIRECTORIES):
        raise PermissionDenied
    obj.is_active = not obj.is_active
    obj.save(update_fields=["is_active", "updated_at"])
    state = "активирована" if obj.is_active else "деактивирована"
    messages.success(request, f"Запись {state}: {obj}")
    return redirect(redirect_to)
