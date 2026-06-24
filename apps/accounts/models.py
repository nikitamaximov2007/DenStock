from django.contrib.auth.models import AbstractUser
from django.db import models

from . import roles


class User(AbstractUser):
    """Кастомная модель пользователя.

    Ставится сразу на Слое 1: менять модель пользователя после старта проекта
    в Django крайне болезненно. Роли реализованы через Django Groups (Слой 2),
    возможности вычисляются из членства в группах (см. roles.py).
    """

    full_name = models.CharField("ФИО", max_length=255, blank=True)

    class Meta:
        verbose_name = "Пользователь"
        verbose_name_plural = "Пользователи"

    def __str__(self) -> str:
        return self.full_name or self.get_username()

    # --- Роли и возможности ---
    @property
    def role_names(self) -> set[str]:
        return set(self.groups.values_list("name", flat=True))

    @property
    def capabilities(self) -> set[str]:
        # Суперпользователь = Администратор: все возможности.
        if self.is_superuser:
            return set(roles.ALL_CAPABILITIES)
        return roles.capabilities_for_roles(self.role_names)

    def has_capability(self, capability: str) -> bool:
        return capability in self.capabilities

    @property
    def is_admin(self) -> bool:
        return self.is_superuser or roles.ADMIN in self.role_names

    @property
    def is_manager(self) -> bool:
        return roles.MANAGER in self.role_names

    @property
    def is_storekeeper(self) -> bool:
        return roles.STOREKEEPER in self.role_names

    @property
    def is_seller(self) -> bool:
        return roles.SELLER in self.role_names

    @property
    def is_viewer(self) -> bool:
        return roles.VIEWER in self.role_names

    @property
    def can_manage_users(self) -> bool:
        return self.has_capability(roles.MANAGE_USERS)

    @property
    def can_manage_directories(self) -> bool:
        return self.has_capability(roles.MANAGE_DIRECTORIES)

    @property
    def can_manage_warehouse(self) -> bool:
        return self.has_capability(roles.MANAGE_WAREHOUSE_STRUCTURE)

    @property
    def can_manage_parts(self) -> bool:
        return self.has_capability(roles.MANAGE_PARTS_CATALOG)

    @property
    def can_manage_batches(self) -> bool:
        return self.has_capability(roles.MANAGE_BATCHES)

    @property
    def can_manage_inventory(self) -> bool:
        return self.has_capability(roles.MANAGE_INVENTORY)

    @property
    def can_view_finance(self) -> bool:
        return self.has_capability(roles.VIEW_FINANCE)

    @property
    def can_view_purchase_cost(self) -> bool:
        return self.has_capability(roles.VIEW_PURCHASE_COST)

    @property
    def can_edit(self) -> bool:
        return self.has_capability(roles.EDIT)

    @property
    def can_confirm_adjustments(self) -> bool:
        return self.has_capability(roles.CONFIRM_ADJUSTMENTS)
