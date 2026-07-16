import re
from pathlib import Path

import pytest
from django.conf import settings
from django.contrib.auth.models import Group
from django.test import RequestFactory
from django.urls import reverse

from apps.accounts import roles
from apps.accounts.context_processors import navigation
from apps.catalog.models import Category, PartNumber, PartType, Unit
from apps.core.models import UnresolvedScan
from apps.returns.models import StockReturn

PASSWORD = "navigation-password"


@pytest.fixture
def make_nav_user(db, django_user_model):
    def _make(username, *, role=None, superuser=False):
        if superuser:
            user = django_user_model.objects.create_superuser(
                username=username,
                password=PASSWORD,
            )
        else:
            user = django_user_model.objects.create_user(
                username=username,
                password=PASSWORD,
            )
        if role:
            user.groups.add(Group.objects.get(name=role))
        return user

    return _make


def _login(client, user):
    client.force_login(user)


def _sidebar_labels(html):
    sidebar = _sidebar(html)
    return re.findall(r'<span class="nav__label">([^<]+)</span>', sidebar)


def _sidebar(html):
    return html.split('id="app-sidebar"', 1)[1].split("</nav>", 1)[0]


def _primary_labels(html):
    primary = _sidebar(html).split("</ul>", 1)[0]
    return re.findall(r'<span class="nav__label">([^<]+)</span>', primary)


def _sidebar_groups(html):
    groups = {}
    pattern = r'<section class="nav__group[^>]*data-nav-group="([^"]+)"[^>]*>(.*?)</section>'
    for key, body in re.findall(pattern, _sidebar(html), flags=re.DOTALL):
        groups[key] = re.findall(r'<span class="nav__label">([^<]+)</span>', body)
    return groups


def _html(client, name, *, query=""):
    return client.get(f"{reverse(name)}{query}").content.decode()


def test_admin_sidebar_has_clean_expandable_sections(client, make_nav_user):
    _login(client, make_nav_user("admin", superuser=True))
    html = _html(client, "dashboard")
    assert _primary_labels(html) == ["Главная", "Поиск"]
    assert _sidebar_groups(html) == {
        "warehouse": [
            "Остатки",
            "Ячейки",
            "Поступление",
            "Перемещение",
            "Инвентаризация",
            "Быстрые действия",
            "История",
        ],
        "sales": ["Продажи", "Резервы", "Возвраты покупателей"],
        "repairs": ["Ремонты", "Возвраты из ремонта"],
        "reports": ["Сводка", "Складские действия / Таможня", "Статистика"],
        "settings": ["Цены", "Пользователи", "Бэкапы"],
    }
    assert html.count('data-nav-group-toggle') == 5
    assert html.count('aria-expanded="true"') >= 5


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            roles.STOREKEEPER,
            {
                "warehouse": [
                    "Остатки",
                    "Ячейки",
                    "Поступление",
                    "Перемещение",
                    "Инвентаризация",
                    "Быстрые действия",
                    "История",
                ],
                "sales": ["Возвраты покупателей"],
                "repairs": ["Ремонты", "Возвраты из ремонта"],
                "reports": ["Сводка", "Складские действия / Таможня"],
            },
        ),
        (
            roles.SELLER,
            {
                "sales": ["Продажи", "Резервы"],
                "repairs": ["Ремонты"],
                "reports": ["Складские действия / Таможня"],
            },
        ),
    ],
)
def test_sidebar_is_capability_aware(client, make_nav_user, role, expected):
    _login(client, make_nav_user(f"user-{role}", role=role))
    html = _html(client, "dashboard")
    assert _primary_labels(html) == ["Главная", "Поиск"]
    assert _sidebar_groups(html) == expected


def test_seller_and_master_share_current_combined_role_menu(client, make_nav_user):
    seller = make_nav_user("seller", role=roles.SELLER)
    master = make_nav_user("master", role=roles.SELLER)
    _login(client, seller)
    seller_labels = _sidebar_groups(_html(client, "dashboard"))
    client.logout()
    _login(client, master)
    assert _sidebar_groups(_html(client, "dashboard")) == seller_labels


def test_plain_user_has_no_empty_or_administrative_sections(client, make_nav_user):
    _login(client, make_nav_user("plain"))
    html = _html(client, "dashboard")
    assert _primary_labels(html) == ["Главная", "Поиск"]
    assert _sidebar_groups(html) == {}
    assert "Настройки" not in html
    assert "data-nav-group=" not in html


def test_unified_search_replaces_general_scanner(client, make_nav_user, db):
    user = make_nav_user("searcher")
    category = Category.objects.create(name="Навигационный тест")
    unit = Unit.objects.get(name="Штука")
    part = PartType.objects.create(name="Универсальная деталь", category=category, unit=unit)
    PartNumber.objects.create(part=part, value="NAV-100", kind=PartNumber.Kind.OEM)
    _login(client, user)

    html = _html(client, "part_search", query="?q=NAV-100")
    assert 'data-scan-input' in html
    assert "Универсальная деталь" in html
    assert "Сканер готов" in html
    assert "scanfield__input" not in html
    assert 'href="/scanner/"' not in html

    old_get = client.get(reverse("scanner"))
    assert old_get.status_code == 302
    assert old_get.url == reverse("part_search")
    old_post = client.post(reverse("scanner"), {"code": " NAV-100\r\n"})
    assert old_post.status_code == 302
    assert old_post.url == f"{reverse('part_search')}?q=NAV-100"
    client.post(reverse("scanner"), {"code": "UNKNOWN-NAV-CODE"})
    assert UnresolvedScan.objects.filter(raw_value="UNKNOWN-NAV-CODE").count() == 1


@pytest.mark.parametrize(
    ("name", "active_label"),
    [
        ("part_list", "Все детали"),
        ("brp_search", "BRP"),
        ("polaris_search", "Polaris"),
    ],
)
def test_catalog_tabs_are_direct_without_restoring_catalog_sidebar(
    client,
    make_nav_user,
    name,
    active_label,
):
    _login(client, make_nav_user(f"catalog-{name}"))
    html = _html(client, name)
    assert f'aria-current="page">{active_label}</a>' in html
    assert "Все детали" in html and "BRP" in html and "Polaris" in html
    assert "Каталог" not in _sidebar(html)


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("balance_list", "Остатки"),
        ("warehouse_index", "Ячейки"),
        ("receipt_list", "Поступление"),
        ("scanner_move", "Перемещение"),
        ("counting_list", "Инвентаризация"),
        ("actions_scan", "Быстрые действия"),
        ("movement_list", "История"),
        ("write_off_list", "Списания"),
    ],
)
def test_warehouse_tabs_use_existing_direct_urls(
    client,
    make_nav_user,
    name,
    label,
):
    _login(client, make_nav_user(f"warehouse-{name}", role=roles.STOREKEEPER))
    html = _html(client, name)
    assert f'aria-current="page">{label}</a>' in html
    assert ">Склад<" in html
    if label == "Списания":
        assert label not in _sidebar_labels(html)
        assert 'data-nav-group="warehouse" data-nav-active="true"' in " ".join(
            _sidebar(html).split()
        )
    else:
        assert label in _sidebar_groups(html)["warehouse"]


def test_receiving_and_inventory_modes_are_nested(client, make_nav_user):
    _login(client, make_nav_user("storekeeper", role=roles.STOREKEEPER))
    receiving = _html(client, "receipt_list")
    assert "Поступления" in receiving
    assert "Партии поставок" in receiving
    assert "Приёмка сканером" in receiving

    counting = _html(client, "counting_list")
    assert "Инвентаризация ячейки" in counting
    assert "Сверочные документы" in counting


def test_items_and_lots_are_inside_stock_navigation(client, make_nav_user):
    _login(client, make_nav_user("viewer", role=roles.VIEWER))
    html = _html(client, "balance_list")
    assert 'href="/inventory/"' in html
    assert 'href="/inventory/lots/"' in html
    assert "Экземпляры" not in _sidebar_labels(html)
    assert "Лоты" not in _sidebar_labels(html)


def test_return_tabs_filter_sources_without_changing_old_journal(
    client,
    make_nav_user,
    db,
):
    _login(client, make_nav_user("returns", superuser=True))
    sale_return = StockReturn.objects.create(
        source_type=StockReturn.SourceType.SALE,
        source_id=101,
    )
    repair_return = StockReturn.objects.create(
        source_type=StockReturn.SourceType.REPAIR_ORDER,
        source_id=202,
    )

    customer_html = _html(client, "return_list", query="?source=sale")
    assert sale_return.number in customer_html
    assert repair_return.number not in customer_html
    assert "Возвраты покупателей" in customer_html

    repair_html = _html(client, "return_list", query="?source=repair")
    assert repair_return.number in repair_html
    assert sale_return.number not in repair_html
    assert "Возвраты из ремонта" in repair_html

    journal_html = _html(client, "return_list")
    assert sale_return.number in journal_html
    assert repair_return.number in journal_html


def test_reports_and_settings_tabs_follow_permissions(client, make_nav_user):
    admin = make_nav_user("admin", superuser=True)
    _login(client, admin)
    reports = _html(client, "reports_dashboard")
    assert "Сводка" in reports
    assert "Складские действия / Таможня" in reports
    assert "Статистика" in reports
    settings = _html(client, "directory_index")
    for label in (
        "Справочники",
        "Цены",
        "Пользователи",
        "Инструменты / Нераспознанные",
        "Бэкапы",
    ):
        assert label in settings

    client.logout()
    _login(client, make_nav_user("storekeeper", role=roles.STOREKEEPER))
    restricted = _html(client, "dashboard")
    assert "settings" not in _sidebar_groups(restricted)
    assert client.get(reverse("price_settings")).status_code == 403
    assert client.get(reverse("statistics_dashboard")).status_code == 403


def test_directories_stay_internal_without_sidebar_entry(client, make_nav_user):
    _login(client, make_nav_user("directory-admin", superuser=True))
    dashboard = _html(client, "dashboard")
    assert _sidebar_groups(dashboard)["settings"] == ["Цены", "Пользователи", "Бэкапы"]
    assert "Справочники" not in _sidebar_labels(dashboard)

    directories = client.get(reverse("directory_index"))
    assert directories.status_code == 200
    html = directories.content.decode()
    assert 'aria-current="page">Справочники</a>' in html
    assert "Справочники" not in _sidebar_labels(html)


def test_specialized_scanner_endpoints_remain_available(client, make_nav_user):
    _login(client, make_nav_user("scanner-storekeeper", role=roles.STOREKEEPER))
    for name in ("scanner_receiving", "scanner_move", "counting_list", "actions_scan"):
        assert client.get(reverse(name)).status_code == 200
    assert client.post(reverse("scanner_resolve"), {"code": ""}).status_code == 400


def test_local_tabs_use_partial_navigation_and_exports_stay_full_navigation():
    partial = (
        Path(settings.BASE_DIR) / "static" / "js" / "partial_navigation.js"
    ).read_text(encoding="utf-8")
    template = (
        Path(settings.BASE_DIR) / "templates" / "partials" / "_section_navigation.html"
    ).read_text(encoding="utf-8")
    actions = (
        Path(settings.BASE_DIR) / "templates" / "actions" / "report.html"
    ).read_text(encoding="utf-8")
    assert 'a[data-partial-link]' in partial
    assert "data-partial-link" in template
    assert "data-full-navigation" in actions
    assert 'link.hasAttribute("data-full-navigation")' in partial


def test_navigation_context_has_constant_role_query_count(
    make_nav_user,
    django_assert_num_queries,
):
    user = make_nav_user("query-admin", role=roles.ADMIN)
    request = RequestFactory().get(reverse("dashboard"))
    request.user = user
    request.resolver_match = None
    with django_assert_num_queries(1):
        context = navigation(request)
    assert len(context["nav_items"]) == 2
    assert [group["key"] for group in context["nav_groups"]] == [
        "warehouse",
        "sales",
        "repairs",
        "reports",
        "settings",
    ]


def test_sidebar_omits_hidden_and_duplicate_navigation_entries(client, make_nav_user):
    _login(client, make_nav_user("hidden-links", superuser=True))
    labels = _sidebar_labels(_html(client, "dashboard"))
    for hidden in (
        "Каталог",
        "Детали",
        "BRP",
        "Polaris",
        "Партии",
        "Лоты",
        "Экземпляры",
        "Нераспознанные",
        "Инструменты / Нераспознанные",
        "Справочники",
        "Списания",
        "Сканер",
        "Поиск детали",
    ):
        assert hidden not in labels
    assert labels.count("Поиск") == 1


def test_active_sidebar_group_is_server_rendered_open(client, make_nav_user):
    _login(client, make_nav_user("active-group", role=roles.STOREKEEPER))
    html = _html(client, "scanner_move")
    sidebar = " ".join(_sidebar(html).split())
    assert 'class="nav__group is-active" data-nav-group="warehouse"' in sidebar
    assert 'data-nav-active="true"' in sidebar
    assert 'href="/scanner/move/" aria-current="page"' in sidebar
