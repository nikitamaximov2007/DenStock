from django.urls import path

from . import views

urlpatterns = [
    path("", views.PartItemListView.as_view(), name="item_list"),
    path("movements/", views.MovementListView.as_view(), name="movement_list"),
    path("movements/<int:pk>/", views.MovementDetailView.as_view(), name="movement_detail"),
    path("balance/", views.BalanceListView.as_view(), name="balance_list"),
    path("<int:pk>/", views.PartItemDetailView.as_view(), name="item_detail"),
    path("<int:pk>/edit/", views.PartItemUpdateView.as_view(), name="item_edit"),
    path("<int:pk>/status/", views.item_status_change, name="item_status_change"),
    path("<int:pk>/receive/", views.item_receive, name="item_receive"),
    path("<int:pk>/move/", views.item_move, name="item_move"),
    path("from-line/<int:line_pk>/", views.item_create, name="item_create"),
    path("from-line/<int:line_pk>/bulk/", views.item_bulk_create, name="item_bulk_create"),
    path("lots/", views.StockLotListView.as_view(), name="lot_list"),
    path("lots/<int:pk>/", views.StockLotDetailView.as_view(), name="lot_detail"),
    path("lots/<int:pk>/edit/", views.lot_edit, name="lot_edit"),
    path("lots/<int:pk>/status/", views.lot_status_change, name="lot_status_change"),
    path("lots/<int:pk>/receive/", views.lot_receive, name="lot_receive"),
    path("lots/<int:pk>/move/", views.lot_move, name="lot_move"),
    path("lots/<int:pk>/adjust/", views.lot_adjust, name="lot_adjust"),
    path("lots/from-line/<int:line_pk>/", views.lot_create, name="lot_create"),
    path(
        "lots/from-line/<int:line_pk>/remaining/",
        views.lot_create_remaining, name="lot_create_remaining",
    ),
]
