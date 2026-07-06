from django.urls import path

from . import views

urlpatterns = [
    path("", views.brp_search, name="brp_search"),
    path("settings/", views.brp_settings, name="brp_settings"),
    path("<int:pk>/promote/", views.brp_promote, name="brp_promote"),
    path("<int:pk>/intake/", views.brp_intake, name="brp_intake"),
]
