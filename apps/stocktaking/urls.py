from django.urls import path

from . import views

urlpatterns = [
    path("", views.inventory_count_list, name="inventory_count_list"),
    path("new/", views.inventory_count_create, name="inventory_count_create"),
    path("initial/<int:pk>/", views.initial_inventory_detail, name="initial_inventory_detail"),
    path("<int:pk>/", views.inventory_count_detail, name="inventory_count_detail"),
    path("<int:pk>/add-lot/", views.inventory_count_add_lot, name="inventory_count_add_lot"),
    path("<int:pk>/complete/", views.inventory_count_complete, name="inventory_count_complete"),
    path("<int:pk>/cancel/", views.inventory_count_cancel, name="inventory_count_cancel"),
    path(
        "lines/<int:pk>/count/",
        views.inventory_count_set_count, name="inventory_count_set_count",
    ),
    path(
        "lines/<int:pk>/remove/",
        views.inventory_count_remove_line, name="inventory_count_remove_line",
    ),
]
