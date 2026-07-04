from django.urls import path

from . import views

urlpatterns = [
    path("", views.receipt_list, name="receipt_list"),
    path("new/", views.receipt_create, name="receipt_create"),
    path("<int:pk>/", views.receipt_detail, name="receipt_detail"),
    path("<int:pk>/edit/", views.receipt_edit, name="receipt_edit"),
    path("<int:pk>/add-line/", views.receipt_add_line, name="receipt_add_line"),
    path("<int:pk>/post/", views.receipt_post, name="receipt_post"),
    path("lines/<int:pk>/edit/", views.receipt_line_edit, name="receipt_line_edit"),
    path("lines/<int:pk>/remove/", views.receipt_remove_line, name="receipt_remove_line"),
]
