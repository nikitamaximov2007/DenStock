from django.urls import path

from . import views

urlpatterns = [
    path("", views.LocationTreeView.as_view(), name="warehouse_index"),
    path("new/", views.LocationCreateView.as_view(), name="location_create"),
    path("<int:pk>/", views.LocationDetailView.as_view(), name="location_detail"),
    path("<int:pk>/edit/", views.LocationUpdateView.as_view(), name="location_edit"),
    path("<int:pk>/toggle/", views.location_toggle, name="location_toggle"),
]
