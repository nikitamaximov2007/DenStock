"""Layer 27 — отдельный корневой маршрут /statistics/ (раздел «Статистика»)."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.statistics_dashboard, name="statistics_dashboard"),
]
