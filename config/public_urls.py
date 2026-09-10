"""The intentionally tiny URL surface for catalog-web."""

from django.http import JsonResponse
from django.urls import path

from apps.catalog import public_views

urlpatterns = [
    path("", public_views.public_root, name="public_catalog_root"),
    path("healthz/", public_views.healthz, name="public_catalog_healthz"),
]


def public_not_found(request, exception):
    return JsonResponse({"detail": "Not found."}, status=404)


handler404 = public_not_found
