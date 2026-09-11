"""The public runtime exposes only catalog routes, under its own middleware stack."""

import pytest
from django.test import Client, override_settings
from django.urls import get_resolver

from apps.catalog.public_settings import PUBLIC_MIDDLEWARE
from tests.public_catalog_support import PUBLIC_HOST

INTERNAL_PATHS = (
    "/admin/",
    "/admin/login/",
    "/login/",
    "/logout/",
    "/accounts/login/",
    "/dashboard/",
    "/quick-actions/",
    "/inventory/",
    "/stock/",
    "/sales/",
    "/repairs/",
    "/write-off/",
    "/writeoffs/",
    "/clients/",
    "/customers/",
    "/reports/",
    "/customs/",
    "/customs-orders/",
    "/directories/",
    "/parts/",
    "/parts/1/",
    "/parts/public-photos/",
    "/backups/",
    "/operations/",
    "/ai-support/",
    "/customer-requests/",
    "/customer-requests/1/",
    "/customer-requests/telegram/webhook/",
    "/api/",
    "/media/part-types/1/x.jpg",
    "/media/",
    "/private_media/x.png",
    "/static/css/app.css",
    "/static/js/app_shell.js",
    "/static/admin/css/base.css",
    "/.env",
    "/.git/config",
    "/healthz/../admin/",
)


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_public_runtime_exposes_only_root_and_health(db):
    client = Client()
    assert client.get("/", HTTP_HOST="catalog.example").status_code == 200
    assert client.get("/healthz/", HTTP_HOST="catalog.example").status_code == 200
    for path in (
        "/admin/", "/login/", "/stock/", "/inventory/", "/quick-actions/",
        "/sales/", "/repairs/", "/reports/", "/clients/", "/customs/", "/media/x.jpg",
    ):
        assert client.get(path, HTTP_HOST="catalog.example").status_code == 404, path


@override_settings(ROOT_URLCONF="config.public_urls", ALLOWED_HOSTS=["catalog.example"])
def test_public_runtime_rejects_unknown_host(db):
    client = Client()
    assert client.get("/", HTTP_HOST="unknown.example").status_code == 400


@pytest.mark.parametrize("path", INTERNAL_PATHS)
def test_internal_routes_are_absent_under_the_real_public_stack(public_client, path):
    for method in ("get", "post"):
        response = getattr(public_client, method)(path)
        assert response.status_code in {404, 405}, (method, path, response.status_code)
        assert "Location" not in response, "no redirect to an internal login"
        body = response.content.decode(errors="ignore")
        assert "DenisStock" not in body and "csrfmiddlewaretoken" not in body


def test_public_resolver_has_only_catalog_routes():
    names = {pattern.name for pattern in get_resolver("config.public_urls").url_patterns}
    assert names == {
        "public_catalog_root",
        "public_catalog_search",
        "public_catalog_part",
        "public_catalog_photo",
        "public_catalog_cart",
        "public_catalog_cart_add",
        "public_catalog_cart_remove",
        "public_catalog_request_form",
        "public_catalog_request_submit",
        "public_catalog_request_success",
        "public_catalog_robots",
        "public_catalog_sitemap",
        "public_catalog_sitemap_parts",
        "public_catalog_healthz",
    }


def test_public_stack_has_no_authentication_or_business_write_guard():
    # The request-wide guard middleware is internal-only. The SQL-level write
    # guard still wraps the one public write (tests/test_public_catalog_requests.py).
    assert "django.contrib.auth.middleware.AuthenticationMiddleware" not in PUBLIC_MIDDLEWARE
    assert "apps.operations.write_guard.BusinessWriteGuardMiddleware" not in PUBLIC_MIDDLEWARE


def test_public_host_header_is_enforced_by_the_real_stack(db):
    from tests.public_catalog_support import public_runtime_settings

    with public_runtime_settings():
        assert Client(HTTP_HOST=PUBLIC_HOST).get("/").status_code == 200
        assert Client(HTTP_HOST="admin.pro-stor.ru").get("/").status_code == 400
        assert Client(HTTP_HOST="evil.example").get("/cart/").status_code == 400


def test_public_static_serves_only_public_assets():
    from django.conf import settings

    public_settings_source = (settings.BASE_DIR / "config" / "settings" / "public.py").read_text()
    assert 'STATICFILES_DIRS = [("public_catalog", BASE_DIR / "static" / "public_catalog")]' in (
        public_settings_source
    )
    assert "FileSystemFinder" in public_settings_source
    assert "AppDirectoriesFinder" not in public_settings_source
    public_dir = settings.BASE_DIR / "static" / "public_catalog"
    assert {path.suffix for path in public_dir.iterdir()} <= {".css", ".svg"}
