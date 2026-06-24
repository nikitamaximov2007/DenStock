from django.urls import path

from . import views

urlpatterns = [
    path("", views.BatchListView.as_view(), name="batch_list"),
    path("new/", views.BatchCreateView.as_view(), name="batch_create"),
    path("<int:pk>/", views.BatchDetailView.as_view(), name="batch_detail"),
    path("<int:pk>/edit/", views.BatchUpdateView.as_view(), name="batch_edit"),
    path("<int:pk>/status/", views.batch_status_change, name="batch_status_change"),
    path("<int:pk>/cost/preview/", views.cost_preview, name="batch_cost_preview"),
    path("<int:pk>/cost/finalize/", views.cost_finalize, name="batch_cost_finalize"),
    path("<int:pk>/lines/add/", views.BatchLineCreateView.as_view(), name="batch_line_add"),
    path("lines/<int:pk>/edit/", views.BatchLineUpdateView.as_view(), name="batch_line_edit"),
    path("lines/<int:pk>/delete/", views.line_delete, name="batch_line_delete"),
]
