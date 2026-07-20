import re
from urllib.parse import urlsplit

from django.conf import settings
from django.urls import Resolver404, resolve

ROUTE_CONTEXT = {
    "dashboard": ("Главная", "Главная"),
    "part_search": ("Поиск", "Поиск"),
    "scanner": ("Поиск", "Общий сканер"),
    "part_list": ("Каталог", "Все детали"),
    "part_detail": ("Каталог", "Карточка детали"),
    "brp_search": ("Каталог", "BRP-каталог"),
    "polaris_search": ("Каталог", "Polaris-каталог"),
    "balance_list": ("Склад", "Остатки"),
    "item_list": ("Склад", "Экземпляры"),
    "item_detail": ("Склад", "Карточка экземпляра"),
    "lot_list": ("Склад", "Лоты"),
    "lot_detail": ("Склад", "Карточка лота"),
    "movement_list": ("Склад", "История"),
    "movement_detail": ("Склад", "Складское движение"),
    "warehouse_index": ("Склад", "Ячейки"),
    "location_detail": ("Склад", "Карточка ячейки"),
    "batch_list": ("Склад", "Партии поставок"),
    "batch_detail": ("Склад", "Карточка партии"),
    "receipt_list": ("Склад", "Поступления"),
    "receipt_create": ("Склад", "Новое поступление"),
    "receipt_detail": ("Склад", "Поступление"),
    "scanner_receiving": ("Склад", "Приёмка сканером"),
    "scanner_move": ("Склад", "Перемещение"),
    "counting_list": ("Склад", "Инвентаризация ячейки"),
    "counting_new": ("Склад", "Новый пересчёт ячейки"),
    "counting_detail": ("Склад", "Пересчёт ячейки"),
    "counting_convert": ("Склад", "Завершение пересчёта"),
    "inventory_count_list": ("Склад", "Сверочные документы"),
    "inventory_count_create": ("Склад", "Новая инвентаризация"),
    "inventory_count_detail": ("Склад", "Сверочный документ"),
    "initial_inventory_detail": ("Склад", "Первичный ввод ячейки"),
    "actions_scan": ("Склад", "Быстрые действия"),
    "actions_report": ("Отчёты", "Складские действия / Таможня"),
    "actions_cancel": ("Отчёты", "Отмена продажи из быстрых действий"),
    "sale_list": ("Продажи", "Продажи"),
    "sale_detail": ("Продажи", "Карточка продажи"),
    "reservation_list": ("Продажи", "Резервы"),
    "reservation_detail": ("Продажи", "Карточка резерва"),
    "repair_order_list": ("Ремонты", "Ремонты"),
    "repair_order_detail": ("Ремонты", "Ремонтный заказ"),
    "return_list": ("Возвраты", "Возвраты"),
    "return_detail": ("Возвраты", "Карточка возврата"),
    "write_off_list": ("Склад", "Списания"),
    "write_off_detail": ("Склад", "Карточка списания"),
    "reports_dashboard": ("Отчёты", "Сводка"),
    "reports_stock": ("Отчёты", "Остатки и низкие остатки"),
    "statistics_dashboard": ("Отчёты", "Статистика"),
    "price_settings": ("Настройки", "Цены"),
    "user_list": ("Настройки", "Пользователи"),
    "user_create": ("Настройки", "Новый пользователь"),
    "user_edit": ("Настройки", "Редактирование пользователя"),
    "unresolved_list": ("Настройки", "Нераспознанные сканы"),
    "operations:backups": ("Настройки", "Бэкапы"),
    "ai_support:home": ("ИИ-поддержка", "ИИ-поддержка"),
    "ai_support:conversation": ("ИИ-поддержка", "Разговор"),
}
ALLOWED_ROUTE_NAMES = frozenset(ROUTE_CONTEXT)

ENTITY_ROUTES = {
    "part_detail": ("деталь", "pk"),
    "item_detail": ("экземпляр", "pk"),
    "lot_detail": ("лот", "pk"),
    "movement_detail": ("движение", "pk"),
    "location_detail": ("ячейка", "pk"),
    "batch_detail": ("партия", "pk"),
    "receipt_detail": ("поступление", "pk"),
    "counting_detail": ("пересчёт", "pk"),
    "counting_convert": ("пересчёт", "pk"),
    "inventory_count_detail": ("инвентаризация", "pk"),
    "initial_inventory_detail": ("первичный ввод", "pk"),
    "actions_cancel": ("складское действие", "pk"),
    "sale_detail": ("продажа", "pk"),
    "reservation_detail": ("резерв", "pk"),
    "repair_order_detail": ("ремонтный заказ", "pk"),
    "return_detail": ("возврат", "pk"),
    "write_off_detail": ("списание", "pk"),
    "user_edit": ("пользователь", "pk"),
}

_VIEWPORT_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_BROWSERS = {"Chrome", "Edge", "Firefox", "Safari", "Other"}


def canonical_public_url() -> str:
    value = str(settings.DENSTOCK_PUBLIC_BASE_URL or "").strip()
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        return ""
    return value.rstrip("/") + "/"


def safe_route_context(raw_path: str) -> dict[str, str]:
    path = (raw_path or "").strip()
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "?" in path
        or "#" in path
        or "\\" in path
        or "://" in path
        or any(ord(char) < 32 for char in path)
    ):
        return {}
    try:
        match = resolve(path)
    except Resolver404:
        return {}
    route_name = match.view_name
    if route_name not in ALLOWED_ROUTE_NAMES:
        return {}
    section, page = ROUTE_CONTEXT[route_name]
    context = {
        "path": path[:500],
        "route_name": route_name,
        "section": section,
        "page": page,
    }
    entity = ENTITY_ROUTES.get(route_name)
    if entity:
        entity_type, kwarg = entity
        value = match.kwargs.get(kwarg)
        if isinstance(value, int) and 0 < value <= 9_223_372_036_854_775_807:
            context["entity_type"] = entity_type
            context["entity_id"] = str(value)
    return context


def safe_diagnostic_snapshot(
    *, user, route_context: dict[str, str], browser_family: str = "", viewport: str = ""
) -> dict:
    browser = browser_family if browser_family in _BROWSERS else ""
    match = _VIEWPORT_RE.fullmatch(viewport or "")
    safe_viewport = ""
    if match and int(match.group(1)) <= 10000 and int(match.group(2)) <= 10000:
        safe_viewport = viewport
    roles = sorted(user.role_names) if not user.is_superuser else ["Администратор"]
    return {
        "path": route_context.get("path", ""),
        "route_name": route_context.get("route_name", ""),
        "roles": roles,
        "browser_family": browser,
        "viewport": safe_viewport,
        "app_commit": str(settings.DENSTOCK_APP_COMMIT or "")[:64],
        "public_base_url": canonical_public_url(),
    }
