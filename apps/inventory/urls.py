from django.urls import path

from . import views

urlpatterns = [
    path("", views.PartItemListView.as_view(), name="item_list"),
    path("<int:pk>/", views.PartItemDetailView.as_view(), name="item_detail"),
    path("<int:pk>/edit/", views.PartItemUpdateView.as_view(), name="item_edit"),
    path("<int:pk>/status/", views.item_status_change, name="item_status_change"),
    path("from-line/<int:line_pk>/", views.item_create, name="item_create"),
    path("from-line/<int:line_pk>/bulk/", views.item_bulk_create, name="item_bulk_create"),
]
