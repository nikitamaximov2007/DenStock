"""v1.1.3 — таблицы и пагинация (P0-2 из аудита).

Списки с paginate_by=50 теперь показывают постраничную навигацию (раньше записи за
50-й были недостижимы). Только шаблоны: view/queryset/бизнес-логику не трогаем.
"""
import pytest
from django.contrib.auth.models import Group
from django.urls import reverse

from apps.accounts import roles
from apps.catalog.models import Category, PartType, Unit
from apps.inventory.models import StockBalance, StockMovement

PASSWORD = "parol-12345"

LIST_ROUTES = [
    "part_list", "item_list", "lot_list", "movement_list", "balance_list", "batch_list",
]


@pytest.fixture
def make_user(db, django_user_model):
    def _make(username, *, role=None, is_superuser=False):
        if is_superuser:
            user = django_user_model.objects.create_superuser(username=username, password=PASSWORD)
        else:
            user = django_user_model.objects.create_user(username=username, password=PASSWORD)
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


@pytest.fixture
def boss(make_user, client):
    make_user("boss", role=roles.MANAGER)
    client.login(username="boss", password=PASSWORD)
    return "boss"


def _make_parts(n):
    cat = Category.objects.create(name="Категория")
    unit = Unit.objects.get(name="Штука")
    for i in range(n):
        PartType.objects.create(
            name=f"Деталь {i:03d}", category=cat, unit=unit,
            tracking_mode=PartType.TrackingMode.SERIAL,
        )


# --- Пагинатор ---------------------------------------------------------------


def test_no_pagination_with_one_page(client, boss):
    _make_parts(5)  # одна страница
    html = client.get(reverse("part_list")).content.decode()
    assert "Вперёд" not in html  # пагинатор не выводится


def test_pagination_controls_with_many(client, boss):
    _make_parts(51)  # две страницы
    html = client.get(reverse("part_list")).content.decode()
    assert "Вперёд" in html
    assert "page=2" in html


def test_second_page_opens_with_back_link(client, boss):
    _make_parts(51)
    resp = client.get(reverse("part_list") + "?page=2")
    assert resp.status_code == 200
    assert "← Назад" in resp.content.decode()


def test_pagination_preserves_filter(client, boss):
    _make_parts(51)
    html = client.get(reverse("part_list") + "?show=all").content.decode()
    # querystring сохраняет текущий фильтр при переходе на следующую страницу.
    assert "show=all" in html
    assert "page=2" in html


# --- Empty states ------------------------------------------------------------


def test_empty_state_on_empty_item_list(client, boss):
    html = client.get(reverse("item_list")).content.decode()
    assert "Экземпляров нет" in html


def test_empty_state_on_empty_batch_list(client, boss):
    html = client.get(reverse("batch_list")).content.decode()
    assert "Партий нет" in html


# --- Доступность всех 6 списков ----------------------------------------------


@pytest.mark.parametrize("route", LIST_ROUTES)
def test_list_opens(client, boss, route):
    assert client.get(reverse(route)).status_code == 200


# --- Read-only относительно склада -------------------------------------------


def test_list_pages_are_read_only(client, boss):
    _make_parts(3)
    mv_before = StockMovement.objects.count()
    bal_before = sorted(StockBalance.objects.values_list("id", "quantity_available"))
    for route in LIST_ROUTES:
        client.get(reverse(route))
    assert StockMovement.objects.count() == mv_before
    assert sorted(StockBalance.objects.values_list("id", "quantity_available")) == bal_before
