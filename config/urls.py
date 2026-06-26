"""Корневые маршруты DenStock."""
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

handler403 = "apps.accounts.views.permission_denied_view"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("apps.accounts.urls")),
    path("directories/", include("apps.catalog.urls")),
    path("directories/", include("apps.suppliers.urls")),
    path("parts/", include("apps.catalog.part_urls")),
    path("warehouse/", include("apps.warehouse.urls")),
    path("batches/", include("apps.procurement.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("sales/", include("apps.sales.urls")),
    path("repairs/", include("apps.repairs.urls")),
    path("", include("apps.core.urls")),
]
