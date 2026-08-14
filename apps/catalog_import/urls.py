from django.urls import path

from . import views

urlpatterns = [
    path("catalog-import/", views.import_list, name="catalog_import_list"),
    path("catalog-import/upload/", views.import_upload, name="catalog_import_upload"),
    path("catalog-import/<int:pk>/", views.import_detail, name="catalog_import_detail"),
    path("catalog-import/<int:pk>/check/", views.import_recheck, name="catalog_import_recheck"),
    path("catalog-import/<int:pk>/apply/", views.import_apply, name="catalog_import_apply"),
    path("catalog-import/<int:pk>/inspect/", views.import_inspect, name="catalog_import_inspect"),
    path("catalog-import/<int:pk>/file/", views.import_download, name="catalog_import_download"),
]
