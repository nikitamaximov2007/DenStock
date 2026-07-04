"""Прокидываем возможности и навигацию в шаблоны (единый источник для меню).

Пункты меню сгруппированы в разделы (nav_groups) и снабжены иконкой и признаком
активности (по текущему пути). Правила видимости и адреса не менялись — только
подача: группировка, иконка, active. stub=True — заглушка будущего слоя.
"""
from django.urls import reverse

# Порядок разделов бокового меню.
GROUP_ORDER = ["Обзор", "Каталог", "Склад", "Операции", "Аналитика", "Администрирование"]


def _nav_items(user):
    """Плоский список пунктов (с group/icon). Гейты и адреса — как раньше."""
    items = []
    add = items.append
    add({"label": "Главная", "url": reverse("dashboard"), "stub": False,
         "icon": "home", "group": "Обзор"})
    add({"label": "Поиск детали", "url": reverse("part_search"), "stub": False,
         "icon": "search", "group": "Обзор"})
    add({"label": "Сканер", "url": reverse("scanner"), "stub": False,
         "icon": "scan", "group": "Обзор"})
    add({"label": "Детали", "url": reverse("part_list"), "stub": False,
         "icon": "box", "group": "Каталог"})
    if user.can_manage_batches or user.can_view_purchase_cost or user.is_storekeeper:
        add({"label": "Партии", "url": reverse("batch_list"), "stub": False,
             "icon": "layers", "group": "Каталог"})
    if user.can_manage_inventory or user.is_viewer:
        add({"label": "Экземпляры", "url": reverse("item_list"), "stub": False,
             "icon": "grid", "group": "Склад"})
        add({"label": "Лоты", "url": reverse("lot_list"), "stub": False,
             "icon": "layers", "group": "Склад"})
        add({"label": "Движения", "url": reverse("movement_list"), "stub": False,
             "icon": "swap", "group": "Склад"})
        add({"label": "Остатки", "url": reverse("balance_list"), "stub": False,
             "icon": "gauge", "group": "Склад"})
    if user.can_manage_inventory:
        add({"label": "Приёмка сканером", "url": reverse("scanner_receiving"), "stub": False,
             "icon": "inbox", "group": "Склад"})
        add({"label": "Перемещение", "url": reverse("scanner_move"), "stub": False,
             "icon": "swap", "group": "Склад"})
    add({"label": "Резервы", "url": reverse("reservation_list"), "stub": False,
         "icon": "bookmark", "group": "Операции"})
    add({"label": "Продажи", "url": reverse("sale_list"), "stub": False,
         "icon": "cart", "group": "Операции"})
    add({"label": "Ремонт", "url": reverse("repair_order_list"), "stub": False,
         "icon": "wrench", "group": "Операции"})
    add({"label": "Возвраты", "url": reverse("return_list"), "stub": False,
         "icon": "undo", "group": "Операции"})
    add({"label": "Списания", "url": reverse("write_off_list"), "stub": False,
         "icon": "trash", "group": "Операции"})
    add({"label": "Инвентаризация", "url": reverse("inventory_count_list"), "stub": False,
         "icon": "clipboard", "group": "Операции"})
    if user.can_view_reports:
        add({"label": "Отчёты", "url": reverse("reports_dashboard"), "stub": False,
             "icon": "chart", "group": "Аналитика"})
    add({"label": "Справочники", "url": reverse("directory_index"), "stub": False,
         "icon": "book", "group": "Каталог"})
    add({"label": "Склад", "url": reverse("warehouse_index"), "stub": False,
         "icon": "warehouse", "group": "Склад"})
    if user.is_storekeeper or user.is_admin:
        add({"label": "Поступление", "url": None, "stub": True,
             "icon": "inbox", "group": "Администрирование"})
    if user.can_view_finance:
        add({"label": "Статистика", "url": reverse("statistics_dashboard"), "stub": False,
             "icon": "gauge", "group": "Аналитика"})
    if user.is_admin or user.is_manager:
        add({"label": "Нераспознанные", "url": reverse("unresolved_list"), "stub": False,
             "icon": "alert", "group": "Администрирование"})
    if user.can_manage_users:
        add({"label": "Пользователи", "url": reverse("user_list"), "stub": False,
             "icon": "users", "group": "Администрирование"})
    if user.is_admin:
        add({"label": "Бэкапы", "url": reverse("operations:backups"), "stub": False,
             "icon": "database", "group": "Администрирование"})
    return items


def _mark_active(items, path):
    """Отмечает один активный пункт — по самому длинному совпадению пути (prefix)."""
    for it in items:
        it["active"] = False
    best, best_len = None, -1
    for it in items:
        url = it.get("url")
        if not url or it["stub"]:
            continue
        if url == path or (url != "/" and path.startswith(url)):
            if len(url) > best_len:
                best, best_len = it, len(url)
    if best is not None:
        best["active"] = True


def _group(items):
    """Собирает пункты в разделы GROUP_ORDER; пустые разделы отбрасываются."""
    groups = []
    for title in GROUP_ORDER:
        bucket = [it for it in items if it["group"] == title]
        if bucket:
            groups.append({"title": title, "items": bucket})
    return groups


def navigation(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"nav_items": [], "nav_groups": [], "caps": set()}
    items = _nav_items(user)
    _mark_active(items, getattr(request, "path", "") or "")
    return {"nav_items": items, "nav_groups": _group(items), "caps": user.capabilities}
