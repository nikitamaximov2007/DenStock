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
]
