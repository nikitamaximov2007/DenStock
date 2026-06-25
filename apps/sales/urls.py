from django.urls import path

from . import views

urlpatterns = [
    path("reservations/", views.reservation_list, name="reservation_list"),
    path("reservations/new/", views.reservation_create, name="reservation_create"),
    path("reservations/<int:pk>/", views.reservation_detail, name="reservation_detail"),
    path(
        "reservations/<int:pk>/add-item/",
        views.reservation_add_item, name="reservation_add_item",
    ),
    path(
        "reservations/<int:pk>/add-lot/",
        views.reservation_add_lot, name="reservation_add_lot",
    ),
    path(
        "reservations/<int:pk>/activate/",
        views.reservation_activate, name="reservation_activate",
    ),
    path(
        "reservations/<int:pk>/cancel/",
        views.reservation_cancel, name="reservation_cancel",
    ),
    path(
        "reservations/lines/<int:pk>/remove/",
        views.reservation_remove_line, name="reservation_remove_line",
    ),
    path(
        "reservations/<int:pk>/sell/",
        views.sale_from_reservation, name="sale_from_reservation",
    ),
    path("sales/", views.sale_list, name="sale_list"),
    path("sales/new/", views.sale_create, name="sale_create"),
    path("sales/<int:pk>/", views.sale_detail, name="sale_detail"),
    path("sales/<int:pk>/add-item/", views.sale_add_item, name="sale_add_item"),
    path("sales/<int:pk>/add-lot/", views.sale_add_lot, name="sale_add_lot"),
    path("sales/<int:pk>/complete/", views.sale_complete, name="sale_complete"),
    path("sales/lines/<int:pk>/remove/", views.sale_remove_line, name="sale_remove_line"),
]
