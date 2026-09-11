"""The intentionally small URL surface of catalog-web.

Nothing internal is routed here: admin, login, stock, sales, repairs,
reports, customs, internal APIs and uploaded media simply do not exist in this
resolver, so they answer 404 rather than an internal login redirect.
"""

from django.urls import path

from apps.catalog import public_views

urlpatterns = [
    path("", public_views.public_root, name="public_catalog_root"),
    path("search/", public_views.public_search, name="public_catalog_search"),
    path("parts/<uuid:public_id>/", public_views.public_part_detail, name="public_catalog_part"),
    path(
        "photos/<uuid:public_id>/<slug:variant>.jpg",
        public_views.public_photo,
        name="public_catalog_photo",
    ),
    path("cart/", public_views.public_cart, name="public_catalog_cart"),
    path("request/", public_views.public_request_form, name="public_catalog_request_form"),
    path(
        "request/submit/",
        public_views.public_request_submit,
        name="public_catalog_request_submit",
    ),
    path(
        "request/success/<uuid:public_id>/",
        public_views.public_request_success,
        name="public_catalog_request_success",
    ),
    path(
        "cart/<uuid:public_id>/add/", public_views.public_cart_add, name="public_catalog_cart_add"
    ),
    path(
        "cart/<uuid:public_id>/remove/",
        public_views.public_cart_remove,
        name="public_catalog_cart_remove",
    ),
    path("robots.txt", public_views.robots_txt, name="public_catalog_robots"),
    path("sitemap.xml", public_views.sitemap_index, name="public_catalog_sitemap"),
    path(
        "sitemaps/parts-<int:number>.xml",
        public_views.sitemap_parts,
        name="public_catalog_sitemap_parts",
    ),
    path("healthz/", public_views.healthz, name="public_catalog_healthz"),
]

handler400 = public_views.bad_request
handler403 = public_views.permission_denied
handler404 = public_views.not_found
handler500 = public_views.server_error
