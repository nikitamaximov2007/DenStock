"""Прокидываем возможности и навигацию в шаблоны (единый источник для меню)."""
from django.urls import reverse


def _nav_items(user):
    """Пункты меню, видимые пользователю. stub=True — заглушка будущего слоя."""
    items = [{"label": "Главная", "url": reverse("dashboard"), "stub": False}]
    items.append({"label": "Поиск детали", "url": reverse("part_search"), "stub": False})
    items.append({"label": "Сканер", "url": reverse("scanner"), "stub": False})
    items.append({"label": "Детали", "url": reverse("part_list"), "stub": False})
    if user.can_manage_batches or user.can_view_purchase_cost or user.is_storekeeper:
        items.append({"label": "Партии", "url": reverse("batch_list"), "stub": False})
    if user.can_manage_inventory or user.is_viewer:
        items.append({"label": "Экземпляры", "url": reverse("item_list"), "stub": False})
        items.append({"label": "Лоты", "url": reverse("lot_list"), "stub": False})
        items.append({"label": "Движения", "url": reverse("movement_list"), "stub": False})
        items.append({"label": "Остатки", "url": reverse("balance_list"), "stub": False})
    if user.can_manage_inventory:
        items.append(
            {"label": "Приёмка сканером", "url": reverse("scanner_receiving"), "stub": False}
        )
    items.append({"label": "Справочники", "url": reverse("directory_index"), "stub": False})
    items.append({"label": "Склад", "url": reverse("warehouse_index"), "stub": False})
    if user.is_storekeeper or user.is_admin:
        items.append({"label": "Поступление", "url": None, "stub": True})
    if user.is_seller or user.is_admin:
        items.append({"label": "Продажа", "url": None, "stub": True})
    if user.can_view_finance:
        items.append({"label": "Статистика", "url": None, "stub": True})
    if user.is_admin or user.is_manager:
        items.append(
            {"label": "Нераспознанные", "url": reverse("unresolved_list"), "stub": False}
        )
    if user.can_manage_users:
        items.append({"label": "Пользователи", "url": reverse("user_list"), "stub": False})
    return items


def navigation(request):
    user = getattr(request, "user", None)
    if not user or not user.is_authenticated:
        return {"nav_items": [], "caps": set()}
    return {"nav_items": _nav_items(user), "caps": user.capabilities}
