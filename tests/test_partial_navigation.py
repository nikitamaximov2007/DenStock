from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse


@pytest.fixture
def nav_client(client, django_user_model):
    user = django_user_model.objects.create_user(
        username="nav-admin", password="password", is_superuser=True
    )
    client.force_login(user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize(
    "url_name",
    [
        "dashboard",
        "balance_list",
        "part_search",
        "receipt_list",
        "scanner_move",
        "counting_list",
        "repair_order_list",
        "price_settings",
        "reports_dashboard",
        "statistics_dashboard",
    ],
)
def test_navigation_destinations_remain_full_direct_pages(nav_client, url_name):
    response = nav_client.get(reverse(url_name))
    assert response.status_code == 200
    html = response.content.decode()
    assert "<!DOCTYPE html>" in html
    assert 'id="app-sidebar"' in html
    assert 'id="content"' in html
    assert 'aria-current="page"' in html


@pytest.mark.django_db
def test_navigation_shell_has_stable_progressive_enhancement_contract(nav_client):
    html = nav_client.get(reverse("dashboard")).content.decode()
    assert 'id="app-sidebar"' in html
    assert "data-partial-navigation" in html
    assert 'id="content" tabindex="-1"' in html
    assert "js/partial_navigation.js" in html
    assert f'action="{reverse("logout")}"' in html
    assert 'method="post" class="topbar__logout"' in html
    assert "data-partial-link" in client_html(nav_client, "part_list")


@pytest.mark.django_db
def test_unauthenticated_navigation_uses_normal_login_redirect(client):
    response = client.get(reverse("statistics_dashboard"))
    assert response.status_code == 302
    assert reverse("login") in response.url


@pytest.mark.django_db
def test_export_links_are_explicit_full_navigation(nav_client):
    reports_html = nav_client.get(reverse("reports_dashboard")).content.decode()
    assert reports_html.count("data-full-navigation") >= 5
    actions_template = (
        Path(settings.BASE_DIR) / "templates" / "actions" / "report.html"
    ).read_text(encoding="utf-8")
    assert "data-full-navigation" in actions_template


def test_partial_navigation_controller_has_safe_fallback_and_history_contract():
    source = (
        Path(settings.BASE_DIR) / "static" / "js" / "partial_navigation.js"
    ).read_text(encoding="utf-8")
    assert 'SIDEBAR_KEY = "denstock.sidebar.scrollTop.v1"' in source
    assert 'event.target.closest("a.nav__link")' in source
    assert 'link.hasAttribute("data-full-navigation")' in source
    assert "event.ctrlKey" in source and "event.metaKey" in source
    assert "response.redirected" in source
    assert 'type.indexOf("text/html")' in source
    assert "new AbortController()" in source
    assert "controller.abort()" in source
    assert "window.history.pushState" in source
    assert 'window.addEventListener("popstate"' in source
    assert 'content.setAttribute("aria-busy"' in source
    assert "fullNavigation(url)" in source
    assert 'new CustomEvent("denstock:page-loaded"' in source


def test_partial_page_initializers_are_idempotent():
    base = Path(settings.BASE_DIR) / "static" / "js"
    assert "idempotentBound" in (base / "app_shell.js").read_text(encoding="utf-8")
    assert "scannerBound" in (base / "scanner.js").read_text(encoding="utf-8")
    assert "scanFocusInputBound" in (base / "scan_focus.js").read_text(encoding="utf-8")
    assert "galleryReady" in (base / "image_gallery.js").read_text(encoding="utf-8")
    assert "priceSettingsReady" in (base / "price_settings.js").read_text(encoding="utf-8")


def client_html(client, url_name):
    return client.get(reverse(url_name)).content.decode()
