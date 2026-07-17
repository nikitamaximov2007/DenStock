"""Роли и возможности DenisStock.

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
MANAGE_RETURNS = "manage_returns"
MANAGE_WRITE_OFFS = "manage_write_offs"
MANAGE_STOCKTAKING = "manage_stocktaking"
VIEW_REPORTS = "can_view_reports"
VIEW_FINANCE = "can_view_finance"
VIEW_PURCHASE_COST = "can_view_purchase_cost"
EDIT = "can_edit"
CONFIRM_ADJUSTMENTS = "can_confirm_adjustments"
# Слой 23: печать складских этикеток (read-only представление существующих кодов).
PRINT_LABELS = "print_labels"
# Слой 24: управление фотографиями деталей/экземпляров (информационный слой).
MANAGE_IMAGES = "manage_images"
USE_AI_SUPPORT = "use_ai_support"
MANAGE_AI_SUPPORT_TICKETS = "manage_ai_support_tickets"

ALL_CAPABILITIES = frozenset(
    {
        MANAGE_USERS, MANAGE_DIRECTORIES, MANAGE_WAREHOUSE_STRUCTURE, MANAGE_PARTS_CATALOG,
        MANAGE_BATCHES, MANAGE_INVENTORY, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS,
        MANAGE_RETURNS, MANAGE_WRITE_OFFS, MANAGE_STOCKTAKING, VIEW_REPORTS, VIEW_FINANCE,
        VIEW_PURCHASE_COST, EDIT, CONFIRM_ADJUSTMENTS, PRINT_LABELS, MANAGE_IMAGES,
        USE_AI_SUPPORT, MANAGE_AI_SUPPORT_TICKETS,
    }
)

# --- Карта «роль → возможности» ---
ROLE_CAPABILITIES = {
    ADMIN: {
        MANAGE_USERS, MANAGE_DIRECTORIES, MANAGE_WAREHOUSE_STRUCTURE, MANAGE_PARTS_CATALOG,
        MANAGE_BATCHES, MANAGE_INVENTORY, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS,
        MANAGE_RETURNS, MANAGE_WRITE_OFFS, MANAGE_STOCKTAKING, VIEW_REPORTS, VIEW_FINANCE,
        VIEW_PURCHASE_COST, EDIT, CONFIRM_ADJUSTMENTS, PRINT_LABELS, MANAGE_IMAGES,
        USE_AI_SUPPORT, MANAGE_AI_SUPPORT_TICKETS,
    },
    MANAGER: {
        MANAGE_DIRECTORIES, MANAGE_WAREHOUSE_STRUCTURE, MANAGE_PARTS_CATALOG, MANAGE_BATCHES,
        MANAGE_INVENTORY, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS, MANAGE_RETURNS,
        MANAGE_WRITE_OFFS, MANAGE_STOCKTAKING, VIEW_REPORTS, VIEW_FINANCE, VIEW_PURCHASE_COST,
        EDIT, CONFIRM_ADJUSTMENTS, PRINT_LABELS, MANAGE_IMAGES, USE_AI_SUPPORT,
        MANAGE_AI_SUPPORT_TICKETS,
    },
    # Кладовщик ведёт приёмку: создаёт/правит экземпляры, но закупочных сумм не видит.
    # Бронь и продажа под клиента — коммерческие действия продавца, кладовщику не выдаём.
    # Выдача в ремонт, возврат, списание и инвентаризация — складские действия, даём.
    # Отчёты видит, но без денежных сумм (нет VIEW_PURCHASE_COST) — складская аналитика.
    # Печать этикеток (Слой 23) — продолжение приёмки/размещения, поэтому выдаём.
    # Фотофиксация состояния/маркировки (Слой 24) — складская работа, поэтому выдаём.
    STOREKEEPER: {
        MANAGE_INVENTORY, MANAGE_REPAIRS, MANAGE_RETURNS, MANAGE_WRITE_OFFS,
        MANAGE_STOCKTAKING, VIEW_REPORTS, EDIT, PRINT_LABELS, MANAGE_IMAGES,
        USE_AI_SUPPORT,
    },
    # Продавец/Мастер создаёт резервы, проводит продажи и ставит детали в ремонт.
    # Возврат на склад НЕ даём (чтобы не было скрытой отмены продажи) — только просмотр.
    # Общий раздел отчётов на Слое 21 не даём.
    SELLER: {EDIT, MANAGE_RESERVATIONS, MANAGE_SALES, MANAGE_REPAIRS, USE_AI_SUPPORT},
    # Наблюдатель — read-only финансовый просмотр, включая отчёты.
    VIEWER: {VIEW_REPORTS, VIEW_FINANCE, VIEW_PURCHASE_COST, USE_AI_SUPPORT},
}


def capabilities_for_roles(role_names) -> set[str]:
    """Сумма возможностей по набору ролей (совмещение ролей складывает права)."""
    caps: set[str] = set()
    for name in role_names:
        caps |= ROLE_CAPABILITIES.get(name, set())
    return caps
