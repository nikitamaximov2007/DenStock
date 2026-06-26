from django.urls import path

from . import views

urlpatterns = [
    path("", views.write_off_list, name="write_off_list"),
    path("new/", views.write_off_create, name="write_off_create"),
    path("<int:pk>/", views.write_off_detail, name="write_off_detail"),
    path("<int:pk>/add-item/", views.write_off_add_item, name="write_off_add_item"),
    path("<int:pk>/add-lot/", views.write_off_add_lot, name="write_off_add_lot"),
    path("<int:pk>/complete/", views.write_off_complete, name="write_off_complete"),
    path("<int:pk>/cancel/", views.write_off_cancel, name="write_off_cancel"),
    path("lines/<int:pk>/remove/", views.write_off_remove_line, name="write_off_remove_line"),
]
