from django.urls import path

from . import views

urlpatterns = [
    path("", views.reports_dashboard, name="reports_dashboard"),
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
