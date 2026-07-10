"""Layer 26 — сквозной smoke по всем страницам продукта.

Дешёвая, но ценная защита: любой обрыв view/шаблона на списковых и индексных
страницах ловится здесь (нет 500). Также фиксирует инварианты оболочки v1.2.x
(desktop sidebar, кнопка мобильного меню, раскрытые группы) и то, что серые пункты
меню остаются заглушками, а в UI бэкапов нет web-restore/upload/import.

Django test client (без тяжёлого браузерного фреймворка).
"""
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles

PASSWORD = "parol-12345"

# GET-страницы навигации и справочников (без аргументов). Все должны отдавать 200.
ADMIN_PAGES = [
    "dashboard",
    "part_search",
    "scanner",
    "part_list",
    "brp_search",
    "price_settings",
    "counting_list",
    "counting_new",
    "actions_scan",
    "actions_report",
    "batch_list",
    "receipt_list",
    "receipt_create",
    "item_list",
    "lot_list",
    "movement_list",
    "balance_list",
    "scanner_receiving",
    "scanner_move",
    "reservation_list",
    "sale_list",
    "repair_order_list",
    "return_list",
    "write_off_list",
    "inventory_count_list",
    "reports_dashboard",
    "reports_stock",
    "statistics_dashboard",
    "directory_index",
    "category_list",
    "manufacturer_list",
    "unit_list",
    "vehicletype_list",
    "vehiclemake_list",
    "vehiclemodel_list",
    "supplier_list",
    "warehouse_index",
    "unresolved_list",
    "user_list",
    "operations:backups",
]


@pytest.fixture
def admin_client(db, django_user_model, client):
    django_user_model.objects.create_superuser(username="boss", password=PASSWORD)
    client.login(username="boss", password=PASSWORD)
    return client


@pytest.fixture
def seller_client(db, django_user_model, client):
    user = django_user_model.objects.create_user(username="prodavec", password=PASSWORD)
    user.groups.add(Group.objects.get(name=roles.SELLER))
    client.login(username="prodavec", password=PASSWORD)
    return client


@pytest.mark.parametrize("name", ADMIN_PAGES)
def test_admin_page_opens(admin_client, name):
    """Каждая страница навигации открывается администратором без ошибок."""
    resp = admin_client.get(reverse(name))
    assert resp.status_code == 200, f"{name} -> {resp.status_code}"


def test_shell_present_on_pages(admin_client):
    """Оболочка v1.2.x на месте: desktop sidebar + кнопка мобильного меню."""
    html = admin_client.get(reverse("dashboard")).content.decode()
    assert 'id="app-sidebar"' in html
    assert 'id="nav-toggle"' in html  # чекбокс мобильного гамбургера
    assert 'class="topbar__menu"' in html


def test_desktop_sidebar_groups_open(admin_client):
    """Группы меню на desktop раскрыты (details ... open) — как нравится заказчику."""
    html = admin_client.get(reverse("dashboard")).content.decode()
    groups = html.count('<details class="nav__group"')
    open_groups = html.count(" open>")
    assert groups >= 3
    assert open_groups >= groups  # каждая группа раскрыта


def test_no_stub_items_left(admin_client):
    """С Layer 28 в меню не осталось заглушек: все пункты — активные ссылки."""
    html = admin_client.get(reverse("dashboard")).content.decode()
    assert "nav__link--soon" not in html
    assert 'href="/statistics/"' in html  # Статистика (Layer 27)
    assert 'href="/receipts/"' in html  # Поступление (Layer 28)


def test_backups_ui_has_no_web_restore(admin_client):
    """Для ОБЫЧНОГО администратора (без allowlist, флаг выключен) в UI бэкапов
    нет восстановления/загрузки/импорта. Защищённый restore для allowlist-
    владельца проверяется в tests/test_web_restore.py."""
    html = admin_client.get(reverse("operations:backups")).content.decode()
    lowered = html.lower()
    assert 'type="file"' not in lowered  # нет формы загрузки бэкапа
    assert 'action="/operations/restore' not in lowered
    assert "мастер восстановления" not in lowered


def test_seller_blocked_from_admin_pages(seller_client):
    """Продавец не попадает в управление пользователями и бэкапы (403)."""
    assert seller_client.get(reverse("user_list")).status_code == 403
    assert seller_client.get(reverse("operations:backups")).status_code == 403


def test_seller_can_open_core_pages(seller_client):
    """Рабочие страницы продавца открываются: поиск, сканер, продажи."""
    for name in ("dashboard", "part_search", "scanner", "part_list", "sale_list"):
        assert seller_client.get(reverse(name)).status_code == 200, name
