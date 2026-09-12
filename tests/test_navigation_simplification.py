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
    assert _primary_labels(html) == ["Поиск", "ИИ-поддержка"]
    assert _sidebar_groups(html) == {
        "warehouse": [
            "Все детали",
            "Остатки",
            "Ячейки",
            "Приёмка сканером",
            "Перемещение",
            "Инвентаризация",
            "Быстрые действия",
            "Клиенты",
            "Ремонты",
            "Запчасти на заказ",
            "Заявки клиентов",
            "Таможенные заказы",
            "История",
        ],
        "reports": [
            "Сводка",
            "Продажи и ремонты",
            "Продажи по клиентам",
            "Ремонты по клиентам",
            "Складские действия / Таможня",
            "Статистика",
        ],
        "settings": ["Импорт каталога", "Цены", "Пользователи", "Бэкапы"],
    }
    assert html.count('data-nav-group-toggle') == 3
    assert html.count('aria-expanded="true"') >= 3


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (
            roles.STOREKEEPER,
            {
                "warehouse": [
                    "Остатки",
                    "Ячейки",
                    "Приёмка сканером",
                    "Перемещение",
                    "Инвентаризация",
                    "Быстрые действия",
                    "Клиенты",
                    "Ремонты",
                    "История",
                ],
                "reports": [
                    "Сводка",
                    "Продажи и ремонты",
                    "Продажи по клиентам",
                    "Ремонты по клиентам",
                    "Складские действия / Таможня",
                ],
            },
        ),
        (
            roles.SELLER,
            {
                # Быстрые действия это основной ежедневный экран продавца и мастера,
                # и право на него у роли есть. Раньше пункта в меню не было, хотя
                # отчёт по тем же действиям показывался: экран существовал, а
                # добраться до него мышью было нельзя. В разделе «Склад» роль видит
                # только эту вкладку, остальные ограничены своими возможностями.
                "warehouse": [
                    "Быстрые действия",
                    "Клиенты",
                    "Ремонты",
                    "Запчасти на заказ",
                    "Заявки клиентов",
                ],
                "reports": ["Складские действия / Таможня"],
            },
        ),
    ],
)
def test_sidebar_is_capability_aware(client, make_nav_user, role, expected):
    _login(client, make_nav_user(f"user-{role}", role=role))
    html = _html(client, "dashboard")
    assert _primary_labels(html) == ["Поиск", "ИИ-поддержка"]
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
    assert _primary_labels(html) == ["Поиск"]
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


def test_parts_sidebar_entry_uses_the_canonical_route_and_is_active(
    client,
    make_nav_user,
):
    _login(client, make_nav_user("parts-sidebar", superuser=True))
    html = _html(client, "part_list")
    sidebar = " ".join(_sidebar(html).split())

    assert _sidebar_groups(html)["warehouse"][0] == "Все детали"
    assert f'href="{reverse("part_list")}" aria-current="page"' in sidebar
    assert 'class="nav__group is-active" data-nav-group="warehouse"' in sidebar
    assert 'href="/parts/new/"' in html


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("balance_list", "Остатки"),
        ("warehouse_index", "Ячейки"),
        ("scanner_receiving", "Приёмка сканером"),
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
    assert "Продажи по клиентам" in reports
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
    assert _sidebar_groups(dashboard)["settings"] == [
        "Импорт каталога",
        "Цены",
        "Пользователи",
        "Бэкапы",
    ]
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
    """Меню это две постоянные строки: права роли и счётчик новых заявок.

    Оба запроса не зависят ни от числа ролей, ни от числа заявок, а боковое и
    локальное меню собираются из одного и того же посчитанного значения.
    """
    user = make_nav_user("query-admin", role=roles.ADMIN)
    request = RequestFactory().get(reverse("dashboard"))
    request.user = user
    request.resolver_match = None
    with django_assert_num_queries(2):
        context = navigation(request)
    assert len(context["nav_items"]) == 2
    assert [group["key"] for group in context["nav_groups"]] == [
        "warehouse",
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


def test_scanner_receiving_is_the_active_sidebar_entry_and_receipt_routes_remain_available(
    client,
    make_nav_user,
):
    _login(client, make_nav_user("scanner-sidebar", role=roles.STOREKEEPER))
    html = _html(client, "scanner_receiving")
    sidebar = " ".join(_sidebar(html).split())

    assert "Поступление" not in _sidebar_labels(html)
    assert "Приёмка сканером" in _sidebar_groups(html)["warehouse"]
    assert f'href="{reverse("scanner_receiving")}" aria-current="page"' in sidebar
    assert client.get(reverse("receipt_list")).status_code == 200
    assert client.get(reverse("receipt_create")).status_code == 200


def test_repairs_section_is_gone_but_repairs_keep_one_entry_inside_the_warehouse_group(
    client,
    make_nav_user,
):
    """Раздела «Ремонты» в меню нет, но сами ремонты остаются в одном клике.

    Убрать раздел целиком значило бы оставить список ремонтов доступным только
    по прямому адресу, а это для сотрудника то же самое, что его отсутствие
    (см. tests/test_navigation_acceptance.py). Поэтому вместо раздела остаётся
    ровно один пункт внутри «Склада», рядом с «Клиентами».
    """
    _login(client, make_nav_user("repair-routes", superuser=True))
    html = _html(client, "dashboard")
    groups = _sidebar_groups(html)

    # Отдельной секции нет, отдельного пункта возвратов из ремонта тоже.
    assert "repairs" not in groups
    assert "Возвраты из ремонта" not in _sidebar_labels(html)

    # Ровно один пункт «Ремонты», и он внутри «Склада».
    assert _sidebar_labels(html).count("Ремонты") == 1
    assert "Ремонты" in groups["warehouse"]
    assert f'href="{reverse("repair_order_list")}"' in _sidebar(html)

    # Маршруты ремонта и возвратов из ремонта остались на месте.
    assert client.get(reverse("repair_order_list")).status_code == 200
    assert client.get(f"{reverse('return_list')}?source=repair").status_code == 200


def test_repairs_sidebar_entry_is_active_on_a_repair_page(client, make_nav_user):
    _login(client, make_nav_user("repair-active", superuser=True))
    html = _html(client, "repair_order_list")
    sidebar = " ".join(_sidebar(html).split())

    assert f'href="{reverse("repair_order_list")}" aria-current="page"' in sidebar
    assert 'data-nav-group="warehouse"' in sidebar


def test_logo_replaces_home_link(client, make_nav_user):
    _login(client, make_nav_user("logo-admin", superuser=True))
    html = _html(client, "dashboard")
    assert 'href="#i-home"' not in _sidebar(html)
    assert "Главная" not in _sidebar_labels(html)
    assert re.search(
        r'<a class="topbar__brand"[^>]*href="' + re.escape(reverse("dashboard"))
        + r'"[^>]*data-home-link[^>]*>\s*<img[^>]*alt="PRO-STOR"',
        html, re.DOTALL,
    )


# --- Оболочка приложения: сайдбар это колонка, а не блок под общей полосой ----------------


def _shell_css():
    return (settings.BASE_DIR / "static" / "css" / "app.css").read_text(encoding="utf-8")


def _desktop_block(css):
    """Правила десктопной раскладки (@media (min-width: 901px)) одним куском."""
    blocks = []
    for start in (m.end() for m in re.finditer(r"@media \(min-width: 901px\) \{", css)):
        depth, i = 1, start
        while depth and i < len(css):
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        blocks.append(css[start:i])
    assert blocks, "нет десктопного блока раскладки"
    return "\n".join(blocks)


def test_the_brand_is_still_the_home_link(client, make_nav_user):
    """Логотип остаётся ссылкой на Главную в обеих оболочках."""
    _login(client, make_nav_user("shell-admin", superuser=True))
    html = _html(client, "dashboard")
    home = reverse("dashboard")
    assert re.search(
        r'<a class="sidebar__brand"[^>]*href="' + re.escape(home) + r'"',
        html, re.DOTALL,
    ), "в сайдбаре нет кликабельного логотипа"
    assert 'class="sidebar__brand nav__link"' not in _sidebar(html), (
        "логотип не должен получать active-стиль пунктов навигации"
    )
    assert re.search(
        r'<a class="topbar__brand"[^>]*href="' + re.escape(home) + r'"[^>]*data-home-link',
        html, re.DOTALL,
    ), "в топбаре нет кликабельного логотипа для мобильной оболочки"


def test_the_separate_home_menu_item_stays_absent(client, make_nav_user):
    _login(client, make_nav_user("shell-admin-2", superuser=True))
    html = _html(client, "dashboard")
    assert "Главная" not in _sidebar_labels(html)
    assert 'href="#i-home"' not in _sidebar(html)


def test_the_sidebar_shell_carries_the_brand(client, make_nav_user):
    """Бренд живёт внутри самого сайдбара, а не поверх него."""
    _login(client, make_nav_user("shell-admin-3", superuser=True))
    sidebar = _sidebar(_html(client, "dashboard"))
    assert "sidebar__brand" in sidebar
    assert sidebar.index("sidebar__brand") < sidebar.index("nav__list--primary"), (
        "логотип должен стоять выше пунктов меню"
    )


def test_the_desktop_sidebar_owns_the_full_height_column():
    """Сайдбар начинается от верха оболочки, а не под общей верхней полосой.

    Проверяется структура правил, а не пиксели: раскладка задаётся тем, что
    сайдбар закреплён на всю высоту от y=0, а топбар и контент сдвинуты на его
    ширину. Раньше сайдбар начинался на высоте топбара, и полоса с
    пользователем проходила над логотипом.
    """
    desktop = " ".join(_desktop_block(_shell_css()).split())
    assert "position: fixed" in desktop and "top: 0" in desktop, (
        "сайдбар больше не закреплён от верха окна"
    )
    assert "height: 100vh" in desktop, "сайдбар не занимает всю высоту окна"
    assert "margin-left: var(--sidebar-w)" in desktop, (
        "топбар и контент не сдвинуты вправо на ширину сайдбара"
    )
    for selector in (".topbar,", ".emergency-banner,", ".layout"):
        assert selector in desktop, f"{selector} не участвует в сдвиге вправо"


def test_the_desktop_shell_uses_no_visual_hacks():
    """Раскладка чинится геометрией, а не наложением логотипа на топбар."""
    desktop = " ".join(_desktop_block(_shell_css()).split())
    assert "translateY" not in desktop, "сдвиг логотипа трансформацией"
    assert "margin-top: -" not in desktop, "логотип поднят отрицательным отступом"
    assert not re.search(r"z-index:\s*(9\d\d|\d{4,})", desktop), "оверлей с огромным z-index"
    assert ".topbar__brand { position: absolute" not in desktop


def test_the_mobile_drawer_is_left_untouched():
    """Мобильная оболочка живёт в своём блоке и остаётся выезжающей панелью."""
    css = " ".join(_shell_css().split())
    mobile = css.split("@media (max-width: 900px)", 1)[1]
    assert "transform: translateX(-100%)" in mobile, "панель перестала выезжать"
    assert ".nav-toggle:checked ~ .layout .sidebar { transform: none; }" in mobile
    assert "position: fixed" in mobile


def test_the_sidebar_keeps_its_thin_scrollbar():
    css = " ".join(_shell_css().split())
    assert "scrollbar-width: thin" in css
    assert ".sidebar::-webkit-scrollbar { width: 7px; }" in css
