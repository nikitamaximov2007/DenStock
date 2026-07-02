from django.urls import path

from . import views

app_name = "operations"

urlpatterns = [
    path("backups/", views.backups_list, name="backups"),
    path("backups/create/", views.backup_create, name="backup_create"),
    path("backups/<str:run_id>/manifest/", views.backup_manifest, name="backup_manifest"),
    path(
        "backups/<str:run_id>/download/<str:filename>/",
        views.backup_download,
        name="backup_download",
    ),
]
