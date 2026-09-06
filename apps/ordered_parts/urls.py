from django.urls import path

from . import views

urlpatterns = [
    path("", views.ordered_part_list, name="ordered_part_list"),
    path("new/", views.ordered_part_create, name="ordered_part_create"),
    path("<int:pk>/edit/", views.ordered_part_edit, name="ordered_part_edit"),
]
