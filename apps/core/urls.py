from django.urls import path

from . import views

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("healthz/", views.healthz, name="healthz"),
    path("scanner/", views.scanner_page, name="scanner"),
    path("scanner/resolve/", views.scanner_resolve, name="scanner_resolve"),
    path("scanner/unresolved/", views.unresolved_list, name="unresolved_list"),
]
