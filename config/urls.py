"""Корневые маршруты DenisStock."""
from django.conf import settings
from django.conf.urls.static import static
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
    path("receipts/", include("apps.receipts.urls")),
    path("brp/", include("apps.brp.urls")),
    path("polaris/", include("apps.polaris.urls")),
    path("inventory/", include("apps.inventory.urls")),
    path("inventory/", include("apps.counting.urls")),
    path("inventory/", include("apps.actions.urls")),
    path("sales/", include("apps.sales.urls")),
    path("repairs/", include("apps.repairs.urls")),
    path("returns/", include("apps.returns.urls")),
    path("write-offs/", include("apps.writeoffs.urls")),
    path("stocktaking/", include("apps.stocktaking.urls")),
    path("reports/", include("apps.reports.urls")),
    path("statistics/", include("apps.reports.statistics_urls")),
    path("labels/", include("apps.labels.urls")),
    path("operations/", include("apps.operations.urls")),
    path("", include("apps.core.urls")),
]

# Слой 24: раздача загруженных media-файлов в DEBUG (в проде — фронт-прокси).
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
