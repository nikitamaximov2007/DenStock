from django.urls import path

from . import views

urlpatterns = [
    path("actions/", views.actions_scan, name="actions_scan"),
    path("actions/cart/scan/", views.actions_cart_scan, name="actions_cart_scan"),
    path("actions/perform/", views.actions_perform, name="actions_perform"),
    path("actions/cart/add/", views.actions_cart_add, name="actions_cart_add"),
    path("actions/cart/update/", views.actions_cart_update, name="actions_cart_update"),
    path("actions/cart/<str:kind>/clear/", views.actions_cart_clear, name="actions_cart_clear"),
    path("actions/cart/complete/", views.actions_cart_complete, name="actions_cart_complete"),
    path("actions/report/", views.actions_report_view, name="actions_report"),
    path("actions/export/", views.actions_export, name="actions_export"),
    path("actions/<int:pk>/cancel/", views.actions_cancel, name="actions_cancel"),
    path(
        "actions/customs/<int:part_id>/",
        views.actions_customs_edit,
        name="actions_customs_edit",
    ),
    path(
        "actions/customs/<int:part_id>/quick-save/",
        views.actions_customs_quick_save,
        name="actions_customs_quick_save",
    ),
]
