from django.urls import path

from . import views

urlpatterns = [
    path("actions/", views.actions_scan, name="actions_scan"),
    path("actions/perform/", views.actions_perform, name="actions_perform"),
    path("actions/report/", views.actions_report_view, name="actions_report"),
    path("actions/export/", views.actions_export, name="actions_export"),
    path("actions/<int:pk>/cancel/", views.actions_cancel, name="actions_cancel"),
    path(
        "actions/customs/<int:part_id>/",
        views.actions_customs_edit,
        name="actions_customs_edit",
    ),
    path(
        "actions/customs/<int:part_id>/application/",
        views.actions_customs_application,
        name="actions_customs_application",
    ),
]
