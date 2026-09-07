from django.urls import path

from . import views

urlpatterns = [
    path("customs-orders/", views.customs_orders_list, name="customs_orders_list"),
    path("customs-orders/<int:pk>/", views.customs_order_detail, name="customs_order_detail"),
    path("customs-orders/select/", views.customs_order_selection, name="customs_order_selection"),
]
