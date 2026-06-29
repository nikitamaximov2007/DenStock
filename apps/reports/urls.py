from django.urls import path

from . import views

urlpatterns = [
    path("", views.reports_dashboard, name="reports_dashboard"),
    path("stock/", views.reports_stock, name="reports_stock"),
]
