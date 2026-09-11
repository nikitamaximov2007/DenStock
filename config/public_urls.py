"""The intentionally tiny URL surface for catalog-web."""

from django.http import JsonResponse
from django.urls import path

from apps.catalog import public_views

urlpatterns = [
    path("", public_views.public_root, name="public_catalog_root"),
    path("search/", public_views.public_search, name="public_catalog_search"),
    path("parts/<uuid:public_id>/", public_views.public_part_detail, name="public_catalog_part"),
    path("cart/", public_views.public_cart, name="public_catalog_cart"),
    path(
        "cart/<uuid:public_id>/add/", public_views.public_cart_add, name="public_catalog_cart_add"
    ),
    path(
        "cart/<uuid:public_id>/remove/",
        public_views.public_cart_remove,
        name="public_catalog_cart_remove",
    ),
    path("robots.txt", public_views.robots_txt, name="public_catalog_robots"),
    path("sitemap.xml", public_views.sitemap_xml, name="public_catalog_sitemap"),
    path("healthz/", public_views.healthz, name="public_catalog_healthz"),
]


def public_not_found(request, exception):
    return JsonResponse({"detail": "Not found."}, status=404)


handler404 = public_not_found
