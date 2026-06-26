from django.urls import path

from . import views

urlpatterns = [
    path("orders/", views.repair_order_list, name="repair_order_list"),
    path("orders/new/", views.repair_order_create, name="repair_order_create"),
    path("orders/<int:pk>/", views.repair_order_detail, name="repair_order_detail"),
    path("orders/<int:pk>/add-item/", views.repair_order_add_item, name="repair_order_add_item"),
    path("orders/<int:pk>/add-lot/", views.repair_order_add_lot, name="repair_order_add_lot"),
    path("orders/<int:pk>/complete/", views.repair_order_complete, name="repair_order_complete"),
    path("orders/<int:pk>/cancel/", views.repair_order_cancel, name="repair_order_cancel"),
    path(
        "orders/lines/<int:pk>/remove/",
        views.repair_order_remove_line, name="repair_order_remove_line",
    ),
]
