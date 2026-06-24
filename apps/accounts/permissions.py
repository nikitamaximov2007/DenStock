"""Точечные проверки доступа: миксины и декораторы (без глобального middleware)."""
from functools import wraps

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied

from . import roles


class CapabilityRequiredMixin:
    """Требует у пользователя заданную возможность. Иначе 403 (handler403)."""

    required_capability: str | None = None

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if self.required_capability and not request.user.has_capability(self.required_capability):
            raise PermissionDenied
        return super().dispatch(request, *args, **kwargs)


class AdminRequiredMixin(CapabilityRequiredMixin):
    required_capability = roles.MANAGE_USERS


class ManageDirectoriesMixin(CapabilityRequiredMixin):
    required_capability = roles.MANAGE_DIRECTORIES


class ManageWarehouseMixin(CapabilityRequiredMixin):
    required_capability = roles.MANAGE_WAREHOUSE_STRUCTURE


class ManagePartsMixin(CapabilityRequiredMixin):
    required_capability = roles.MANAGE_PARTS_CATALOG


def capability_required(capability: str):
    def decorator(view_func):
        @wraps(view_func)
        def _wrapped(request, *args, **kwargs):
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            if not request.user.has_capability(capability):
                raise PermissionDenied
            return view_func(request, *args, **kwargs)

        return _wrapped

    return decorator


def admin_required(view_func):
    return capability_required(roles.MANAGE_USERS)(view_func)
