"""Роли и возможности DenStock.

Единственная точка, где задаётся соответствие «роль → возможности».
Роли — это Django Groups (пользователь может состоять в нескольких, возможности
складываются). Здесь нет per-model RBAC: только набор именованных возможностей,
которыми позже управляются экраны и навигация.
"""

# --- Названия ролей (= имена групп) ---
ADMIN = "Администратор"
MANAGER = "Руководитель"
STOREKEEPER = "Кладовщик"
SELLER = "Продавец/Мастер"
VIEWER = "Наблюдатель"

ALL_ROLES = [ADMIN, MANAGER, STOREKEEPER, SELLER, VIEWER]

# --- Возможности ---
MANAGE_USERS = "manage_users"
MANAGE_DIRECTORIES = "manage_directories"
MANAGE_WAREHOUSE_STRUCTURE = "manage_warehouse_structure"
MANAGE_PARTS_CATALOG = "manage_parts_catalog"
MANAGE_BATCHES = "manage_batches"
MANAGE_INVENTORY = "manage_inventory"
MANAGE_RESERVATIONS = "manage_reservations"
MANAGE_SALES = "manage_sales"
MANAGE_REPAIRS = "manage_repairs"
VIEW_FINANCE = "can_view_finance"
VIEW_PURCHASE_COST = "can_view_purchase_cost"
EDIT = "can_edit"
CONFIRM_ADJUSTMENTS = "can_confirm_adjustments"

ALL_CAPABILITIES = frozenset(
    {
        MANAGE_USERS, MANAGE_DIRECTORIES, MANAGE_WAREHOUSE_STRUCTURE, MANAGE_PARTS_CATALOG,
        MANAGE_BATCHES, MANAGE_INVENTORY, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS,
        VIEW_FINANCE, VIEW_PURCHASE_COST, EDIT, CONFIRM_ADJUSTMENTS,
    }
)

# --- Карта «роль → возможности» ---
ROLE_CAPABILITIES = {
    ADMIN: {
        MANAGE_USERS, MANAGE_DIRECTORIES, MANAGE_WAREHOUSE_STRUCTURE, MANAGE_PARTS_CATALOG,
        MANAGE_BATCHES, MANAGE_INVENTORY, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS,
        VIEW_FINANCE, VIEW_PURCHASE_COST, EDIT, CONFIRM_ADJUSTMENTS,
    },
    MANAGER: {
        MANAGE_DIRECTORIES, MANAGE_WAREHOUSE_STRUCTURE, MANAGE_PARTS_CATALOG, MANAGE_BATCHES,
        MANAGE_INVENTORY, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS, VIEW_FINANCE,
        VIEW_PURCHASE_COST, EDIT, CONFIRM_ADJUSTMENTS,
    },
    # Кладовщик ведёт приёмку: создаёт/правит экземпляры, но закупочных сумм не видит.
    # Бронь и продажа под клиента — коммерческие действия продавца, кладовщику не выдаём.
    # Выдача в ремонт — складское действие (снять деталь с полки), кладовщику даём.
    STOREKEEPER: {MANAGE_INVENTORY, MANAGE_REPAIRS, EDIT},
    # Продавец/Мастер создаёт резервы, проводит продажи и ставит детали в ремонт.
    SELLER: {EDIT, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS},
    VIEWER: {VIEW_FINANCE, VIEW_PURCHASE_COST},  # просмотр без редактирования
}


def capabilities_for_roles(role_names) -> set[str]:
    """Сумма возможностей по набору ролей (совмещение ролей складывает права)."""
    caps: set[str] = set()
    for name in role_names:
        caps |= ROLE_CAPABILITIES.get(name, set())
    return caps
