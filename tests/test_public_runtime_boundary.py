from django.test import Client, override_settings


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
