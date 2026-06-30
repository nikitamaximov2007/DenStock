from django.urls import path

from . import views

urlpatterns = [
    path("item/<int:pk>/", views.item_label, name="label_item"),
    path("items/", views.items_label, name="label_items"),
    path("location/<int:pk>/", views.location_label, name="label_location"),
    path("part/<int:pk>/", views.part_label, name="label_part"),
]
