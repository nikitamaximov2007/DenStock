from django.urls import path

from . import views

urlpatterns = [
    path("customers/", views.customer_list, name="customer_list"),
    path("customers/new/", views.customer_create, name="customer_create"),
    path(
        "customers/legacy-link/",
        views.legacy_customer_link,
        name="legacy_customer_link",
    ),
    path("customers/<int:pk>/", views.customer_detail, name="customer_detail"),
    path("customers/<int:pk>/edit/", views.customer_edit, name="customer_edit"),
    path(
        "customers/<int:pk>/compare/<int:other_pk>/",
        views.customer_compare,
        name="customer_compare",
    ),
    path(
        "customers/<int:pk>/compare/<int:other_pk>/confirm/",
        views.customer_merge_confirm,
        name="customer_merge_confirm",
    ),
    path("customers/merge/", views.customer_merge_apply, name="customer_merge_apply"),
]
