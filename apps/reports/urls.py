from django.urls import path

from . import views

urlpatterns = [
    path("", views.reports_dashboard, name="reports_dashboard"),
    path("sales-by-client/", views.sales_by_client, name="reports_sales_by_client"),
    path(
        "sales-by-client/customer/",
        views.sales_by_client_detail,
        name="reports_sales_by_client_detail",
    ),
    path(
        "sales-by-client/operations/",
        views.sales_by_client_operations,
        name="reports_sales_by_client_operations",
    ),
    path("repairs-by-client/", views.repairs_by_client, name="reports_repairs_by_client"),
    path(
        "repairs-by-client/customer/",
        views.repairs_by_client_detail,
        name="reports_repairs_by_client_detail",
    ),
    path(
        "repairs-by-client/operations/",
        views.repairs_by_client_operations,
        name="reports_repairs_by_client_operations",
    ),
    path("stock/", views.reports_stock, name="reports_stock"),
    # Слой 22: CSV-экспорт (отдельный endpoint на отчёт)
    path("export/sales.csv", views.export_sales, name="reports_export_sales"),
    path("export/returns.csv", views.export_returns, name="reports_export_returns"),
    path("export/repairs.csv", views.export_repairs, name="reports_export_repairs"),
    path("export/writeoffs.csv", views.export_writeoffs, name="reports_export_writeoffs"),
    path("export/stocktaking.csv", views.export_stocktaking, name="reports_export_stocktaking"),
    path("export/stock.csv", views.export_stock, name="reports_export_stock"),
    path("export/low-stock.csv", views.export_low_stock, name="reports_export_low_stock"),
]
