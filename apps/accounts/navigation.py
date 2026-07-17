"""Build capability-aware primary and local navigation for DenisStock."""

from django.urls import reverse

from . import roles


class _NavAccess:
    """Resolve role capabilities once so rendering the shell adds one role query."""

    _ATTRS = {
        "can_manage_directories": roles.MANAGE_DIRECTORIES,
        "can_manage_parts": roles.MANAGE_PARTS_CATALOG,
        "can_manage_users": roles.MANAGE_USERS,
        "can_manage_inventory": roles.MANAGE_INVENTORY,
        "can_manage_batches": roles.MANAGE_BATCHES,
        "can_manage_write_offs": roles.MANAGE_WRITE_OFFS,
        "can_manage_stocktaking": roles.MANAGE_STOCKTAKING,
        "can_manage_sales": roles.MANAGE_SALES,
        "can_manage_reservations": roles.MANAGE_RESERVATIONS,
        "can_manage_returns": roles.MANAGE_RETURNS,
        "can_manage_repairs": roles.MANAGE_REPAIRS,
        "can_view_reports": roles.VIEW_REPORTS,
        "can_view_finance": roles.VIEW_FINANCE,
        "can_view_purchase_cost": roles.VIEW_PURCHASE_COST,
        "can_use_ai_support": roles.USE_AI_SUPPORT,
        "can_manage_ai_support_tickets": roles.MANAGE_AI_SUPPORT_TICKETS,
    }

    def __init__(self, user):
        self.role_names = set() if user.is_superuser else user.role_names
        self.capabilities = (
            set(roles.ALL_CAPABILITIES)
            if user.is_superuser
            else roles.capabilities_for_roles(self.role_names)
        )
        self.is_admin = user.is_superuser or roles.ADMIN in self.role_names
        self.is_manager = roles.MANAGER in self.role_names
        self.is_storekeeper = roles.STOREKEEPER in self.role_names
        self.is_viewer = roles.VIEWER in self.role_names
        for attr, capability in self._ATTRS.items():
            setattr(self, attr, capability in self.capabilities)


def _item(key, label, url, icon, *, active=False):
    return {
        "key": key,
        "label": label,
        "url": url,
        "icon": icon,
        "stub": False,
        "active": active,
    }


def _tab(label, url, *, active=False, sidebar_key="", icon=""):
    return {
        "label": label,
        "url": url,
        "active": active,
        "sidebar_key": sidebar_key,
        "icon": icon,
    }


def _group(key, label, icon, tabs, *, active=False):
    items = [tab for tab in tabs if tab["sidebar_key"]]
    if not items:
        return None
    for item in items:
        item["active"] = active and item["active"]
    return {
        "key": key,
        "label": label,
        "icon": icon,
        "items": items,
        "active": active,
    }


def _can_use_actions(user):
    return user.can_manage_sales or user.can_manage_reservations or user.can_manage_repairs


def _can_open_warehouse(user):
    return (
        user.can_manage_inventory
        or user.is_viewer
        or user.can_manage_batches
        or user.can_manage_write_offs
        or user.can_manage_stocktaking
    )


def _settings_tabs(user, path):
    tabs = []
    if user.can_manage_directories:
        tabs.append(
            _tab(
                "Справочники",
                reverse("directory_index"),
                active=path.startswith("/directories/")
                and not path.startswith("/directories/price-settings/"),
            )
        )
    if user.can_manage_parts:
        tabs.append(
            _tab(
                "Цены",
                reverse("price_settings"),
                sidebar_key="prices",
                icon="gauge",
                active=path.startswith("/directories/price-settings/"),
            )
        )
    if user.can_manage_users:
        tabs.append(
            _tab(
                "Пользователи",
                reverse("user_list"),
                sidebar_key="users",
                icon="users",
                active=path.startswith("/users/"),
            )
        )
    if user.is_admin or user.is_manager:
        tabs.append(
            _tab(
                "Инструменты / Нераспознанные",
                reverse("unresolved_list"),
                active=path.startswith("/scanner/unresolved/"),
            )
        )
    if user.is_admin:
        tabs.append(
            _tab(
                "Бэкапы",
                reverse("operations:backups"),
                sidebar_key="backups",
                icon="database",
                active=path.startswith("/operations/backups/"),
            )
        )
    return tabs


def _section_key(request):
    explicit = getattr(request, "navigation_section", "")
    if explicit:
        return explicit
    path = request.path
    source = request.GET.get("source") or request.GET.get("section")

    if path == "/":
        return "home"
    if path.startswith("/ai-support/"):
        return "ai-support"
    if path.startswith("/scanner/unresolved/"):
        return "settings"
    if path.startswith("/inventory/actions/report/") or path.startswith(
        "/inventory/actions/customs/"
    ):
        return "reports"
    if path.startswith("/returns/"):
        if source in {"sale", "sales"}:
            return "sales"
        if source in {"repair", "repairs"}:
            return "repairs"
        return "warehouse"
    if path.startswith(("/search/", "/scanner/")) and not path.startswith(
        ("/scanner/receiving/", "/scanner/move/")
    ):
        return "search"
    if path.startswith(("/parts/", "/brp/", "/polaris/")):
        return "catalog"
    if path.startswith("/sales/"):
        return "sales"
    if path.startswith("/repairs/"):
        return "repairs"
    if path.startswith(("/reports/", "/statistics/")):
        return "reports"
    if path.startswith(("/directories/", "/users/", "/operations/backups/")):
        return "settings"
    if path.startswith(
        (
            "/inventory/",
            "/warehouse/",
            "/batches/",
            "/receipts/",
            "/scanner/receiving/",
            "/scanner/move/",
            "/write-offs/",
            "/stocktaking/",
        )
    ):
        return "warehouse"
    return ""


def _primary_items(active_key, user):
    items = [
        _item("home", "Главная", reverse("dashboard"), "home", active=active_key == "home"),
        _item(
            "search",
            "Поиск",
            reverse("part_search"),
            "search",
            active=active_key == "search",
        ),
    ]
    if user.can_use_ai_support:
        items.append(
            _item(
                "ai-support",
                "ИИ-поддержка",
                reverse("ai_support:home"),
                "message",
                active=active_key == "ai-support",
            )
        )
    return items


def _catalog_tabs(path):
    return [
        _tab("Все детали", reverse("part_list"), active=path.startswith("/parts/")),
        _tab("BRP", reverse("brp_search"), active=path.startswith("/brp/")),
        _tab("Polaris", reverse("polaris_search"), active=path.startswith("/polaris/")),
    ]


def _warehouse_tabs(user, path):
    tabs = []
    if user.can_manage_inventory or user.is_viewer:
        tabs.append(
            _tab(
                "Остатки",
                reverse("balance_list"),
                sidebar_key="balances",
                icon="gauge",
                active=path.startswith(("/inventory/balance/", "/inventory/lots/"))
                or (
                    path.startswith("/inventory/")
                    and not path.startswith(
                        (
                            "/inventory/movements/",
                            "/inventory/counting/",
                            "/inventory/actions/",
                        )
                    )
                ),
            )
        )
        tabs.append(
            _tab(
                "Ячейки",
                reverse("warehouse_index"),
                sidebar_key="locations",
                icon="warehouse",
                active=path.startswith("/warehouse/"),
            )
        )
    if user.can_manage_inventory or user.can_manage_batches:
        tabs.append(
            _tab(
                "Поступление",
                reverse("receipt_list"),
                sidebar_key="receiving",
                icon="inbox",
                active=path.startswith(("/receipts/", "/batches/", "/scanner/receiving/")),
            )
        )
    if user.can_manage_inventory:
        tabs.append(
            _tab(
                "Перемещение",
                reverse("scanner_move"),
                sidebar_key="movement",
                icon="swap",
                active=path.startswith("/scanner/move/"),
            )
        )
    if user.can_manage_inventory or user.can_manage_stocktaking:
        inventory_url = (
            reverse("counting_list")
            if user.can_manage_inventory
            else reverse("inventory_count_list")
        )
        tabs.append(
            _tab(
                "Инвентаризация",
                inventory_url,
                sidebar_key="stocktaking",
                icon="clipboard",
                active=path.startswith(("/inventory/counting/", "/stocktaking/")),
            )
        )
    if _can_use_actions(user):
        tabs.append(
            _tab(
                "Быстрые действия",
                reverse("actions_scan"),
                sidebar_key="quick-actions",
                icon="scan",
                active=path.startswith("/inventory/actions/")
                and not path.startswith("/inventory/actions/report/"),
            )
        )
    if user.can_manage_inventory or user.is_viewer:
        tabs.append(
            _tab(
                "История",
                reverse("movement_list"),
                sidebar_key="history",
                icon="swap",
                active=path.startswith("/inventory/movements/") or path == reverse("return_list"),
            )
        )
    if user.can_manage_write_offs:
        tabs.append(
            _tab(
                "Списания",
                reverse("write_off_list"),
                active=path.startswith("/write-offs/"),
            )
        )
    return tabs


def _warehouse_subtabs(user, path):
    if path.startswith(("/receipts/", "/batches/", "/scanner/receiving/")):
        tabs = []
        if user.can_manage_inventory:
            tabs.append(
                _tab("Поступления", reverse("receipt_list"), active=path.startswith("/receipts/"))
            )
        if user.can_manage_batches or user.can_view_purchase_cost or user.is_storekeeper:
            tabs.append(
                _tab(
                    "Партии поставок",
                    reverse("batch_list"),
                    active=path.startswith("/batches/"),
                )
            )
        if user.can_manage_inventory:
            tabs.append(
                _tab(
                    "Приёмка сканером",
                    reverse("scanner_receiving"),
                    active=path.startswith("/scanner/receiving/"),
                )
            )
        return tabs
    if path.startswith(("/inventory/balance/", "/inventory/lots/")) or (
        path.startswith("/inventory/")
        and not path.startswith(
            (
                "/inventory/movements/",
                "/inventory/counting/",
                "/inventory/actions/",
            )
        )
    ):
        return [
            _tab("Остатки", reverse("balance_list"), active=path.startswith("/inventory/balance/")),
            _tab(
                "Экземпляры",
                reverse("item_list"),
                active=path.startswith("/inventory/")
                and not path.startswith(
                    (
                        "/inventory/balance/",
                        "/inventory/lots/",
                        "/inventory/movements/",
                        "/inventory/counting/",
                        "/inventory/actions/",
                    )
                ),
            ),
            _tab("Лоты", reverse("lot_list"), active=path.startswith("/inventory/lots/")),
        ]
    if path.startswith(("/inventory/counting/", "/stocktaking/")):
        tabs = []
        if user.can_manage_inventory:
            tabs.append(
                _tab(
                    "Инвентаризация ячейки",
                    reverse("counting_list"),
                    active=path.startswith("/inventory/counting/"),
                )
            )
        if user.can_manage_stocktaking:
            tabs.append(
                _tab(
                    "Сверочные документы",
                    reverse("inventory_count_list"),
                    active=path.startswith("/stocktaking/"),
                )
            )
        return tabs
    return []


def _sales_tabs(user, path, source):
    tabs = []
    if user.can_manage_sales:
        tabs.append(
            _tab(
                "Продажи",
                reverse("sale_list"),
                sidebar_key="sales",
                icon="cart",
                active=path.startswith("/sales/sales/"),
            )
        )
    if user.can_manage_reservations:
        tabs.append(
            _tab(
                "Резервы",
                reverse("reservation_list"),
                sidebar_key="reservations",
                icon="bookmark",
                active=path.startswith("/sales/reservations/"),
            )
        )
    if user.can_manage_returns:
        tabs.append(
            _tab(
                "Возвраты покупателей",
                f"{reverse('return_list')}?source=sale",
                sidebar_key="customer-returns",
                icon="undo",
                active=path.startswith("/returns/") and source in {"sale", "sales"},
            )
        )
    return tabs


def _repairs_tabs(user, path, source):
    tabs = []
    if user.can_manage_repairs:
        tabs.append(
            _tab(
                "Ремонты",
                reverse("repair_order_list"),
                sidebar_key="repairs",
                icon="wrench",
                active=path.startswith("/repairs/"),
            )
        )
    if user.can_manage_returns:
        tabs.append(
            _tab(
                "Возвраты из ремонта",
                f"{reverse('return_list')}?source=repair",
                sidebar_key="repair-returns",
                icon="undo",
                active=path.startswith("/returns/") and source in {"repair", "repairs"},
            )
        )
    return tabs


def _reports_tabs(user, path):
    tabs = []
    if user.can_view_reports:
        tabs.append(
            _tab(
                "Сводка",
                reverse("reports_dashboard"),
                sidebar_key="summary",
                icon="chart",
                active=path.startswith("/reports/"),
            )
        )
    if _can_use_actions(user):
        tabs.append(
            _tab(
                "Складские действия / Таможня",
                reverse("actions_report"),
                sidebar_key="warehouse-actions",
                icon="clipboard",
                active=path.startswith("/inventory/actions/report/")
                or path.startswith("/inventory/actions/customs/"),
            )
        )
    if user.can_view_finance:
        tabs.append(
            _tab(
                "Статистика",
                reverse("statistics_dashboard"),
                sidebar_key="statistics",
                icon="gauge",
                active=path.startswith("/statistics/"),
            )
        )
    return tabs


def _local_tabs(request, section, user):
    path = request.path
    source = (
        getattr(request, "navigation_source", "")
        or request.GET.get("source")
        or request.GET.get("section")
    )
    if section == "catalog":
        return _catalog_tabs(path), []
    if section == "warehouse":
        return _warehouse_tabs(user, path), _warehouse_subtabs(user, path)
    if section == "sales":
        return _sales_tabs(user, path, source), []
    if section == "repairs":
        return _repairs_tabs(user, path, source), []
    if section == "reports":
        return _reports_tabs(user, path), []
    if section == "settings":
        return _settings_tabs(user, path), []
    return [], []


def _sidebar_groups(request, section, user):
    path = request.path
    source = (
        getattr(request, "navigation_source", "")
        or request.GET.get("source")
        or request.GET.get("section")
    )
    candidates = [
        (
            _group(
                "warehouse",
                "Склад",
                "warehouse",
                _warehouse_tabs(user, path),
                active=section == "warehouse",
            )
            if _can_open_warehouse(user)
            else None
        ),
        _group(
            "sales",
            "Продажи",
            "cart",
            _sales_tabs(user, path, source),
            active=section == "sales",
        ),
        _group(
            "repairs",
            "Ремонты",
            "wrench",
            _repairs_tabs(user, path, source),
            active=section == "repairs",
        ),
        _group(
            "reports",
            "Отчёты",
            "chart",
            _reports_tabs(user, path),
            active=section == "reports",
        ),
        _group(
            "settings",
            "Настройки",
            "gauge",
            _settings_tabs(user, path),
            active=section == "settings",
        ),
    ]
    return [group for group in candidates if group]


def navigation(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {
            "nav_items": [],
            "nav_groups": [],
            "section_tabs": [],
            "section_subtabs": [],
            "caps": set(),
        }
    access = _NavAccess(user)
    section = _section_key(request)
    section_tabs, section_subtabs = _local_tabs(request, section, access)
    nav_items = _primary_items(section, access)
    return {
        "nav_items": nav_items,
        "nav_groups": _sidebar_groups(request, section, access),
        "section_tabs": section_tabs,
        "section_subtabs": section_subtabs,
        "active_section": section,
        "caps": access.capabilities,
    }
