from django.urls import path

from . import views

urlpatterns = [
    path("", views.polaris_search, name="polaris_search"),
    path("settings/", views.polaris_settings, name="polaris_settings"),
    path("<int:pk>/promote/", views.polaris_promote, name="polaris_promote"),
    path("<int:pk>/intake/", views.polaris_intake, name="polaris_intake"),
]

