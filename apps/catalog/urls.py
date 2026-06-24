from django.urls import path

from . import views

urlpatterns = [
    path("", views.DirectoryIndexView.as_view(), name="directory_index"),
    # Категории
    path("categories/", views.CategoryListView.as_view(), name="category_list"),
    path("categories/new/", views.CategoryCreateView.as_view(), name="category_create"),
    path("categories/<int:pk>/edit/", views.CategoryUpdateView.as_view(), name="category_edit"),
    path("categories/<int:pk>/toggle/", views.category_toggle, name="category_toggle"),
    # Производители
    path("manufacturers/", views.ManufacturerListView.as_view(), name="manufacturer_list"),
    path("manufacturers/new/", views.ManufacturerCreateView.as_view(), name="manufacturer_create"),
    path(
        "manufacturers/<int:pk>/edit/",
        views.ManufacturerUpdateView.as_view(),
        name="manufacturer_edit",
    ),
    path(
        "manufacturers/<int:pk>/toggle/", views.manufacturer_toggle, name="manufacturer_toggle"
    ),
    # Единицы измерения
    path("units/", views.UnitListView.as_view(), name="unit_list"),
    path("units/new/", views.UnitCreateView.as_view(), name="unit_create"),
    path("units/<int:pk>/edit/", views.UnitUpdateView.as_view(), name="unit_edit"),
    path("units/<int:pk>/toggle/", views.unit_toggle, name="unit_toggle"),
    # Виды техники
    path("vehicle-types/", views.VehicleTypeListView.as_view(), name="vehicletype_list"),
    path("vehicle-types/new/", views.VehicleTypeCreateView.as_view(), name="vehicletype_create"),
    path(
        "vehicle-types/<int:pk>/edit/",
        views.VehicleTypeUpdateView.as_view(),
        name="vehicletype_edit",
    ),
    path("vehicle-types/<int:pk>/toggle/", views.vehicletype_toggle, name="vehicletype_toggle"),
    # Марки техники
    path("vehicle-makes/", views.VehicleMakeListView.as_view(), name="vehiclemake_list"),
    path("vehicle-makes/new/", views.VehicleMakeCreateView.as_view(), name="vehiclemake_create"),
    path(
        "vehicle-makes/<int:pk>/edit/",
        views.VehicleMakeUpdateView.as_view(),
        name="vehiclemake_edit",
    ),
    path("vehicle-makes/<int:pk>/toggle/", views.vehiclemake_toggle, name="vehiclemake_toggle"),
    # Модели техники
    path("vehicle-models/", views.VehicleModelListView.as_view(), name="vehiclemodel_list"),
    path(
        "vehicle-models/new/", views.VehicleModelCreateView.as_view(), name="vehiclemodel_create"
    ),
    path(
        "vehicle-models/<int:pk>/edit/",
        views.VehicleModelUpdateView.as_view(),
        name="vehiclemodel_edit",
    ),
    path("vehicle-models/<int:pk>/toggle/", views.vehiclemodel_toggle, name="vehiclemodel_toggle"),
]
