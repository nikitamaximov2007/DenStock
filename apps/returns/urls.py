from django.urls import path

from . import views

urlpatterns = [
    path("", views.return_list, name="return_list"),
    path("new/", views.return_create, name="return_create"),
    path("<int:pk>/", views.return_detail, name="return_detail"),
    path("<int:pk>/add-line/", views.return_add_line, name="return_add_line"),
    path("<int:pk>/complete/", views.return_complete, name="return_complete"),
    path("<int:pk>/cancel/", views.return_cancel, name="return_cancel"),
    path(
        "lines/<int:pk>/status/",
        views.return_update_line_status,
        name="return_update_line_status",
    ),
    path("lines/<int:pk>/remove/", views.return_remove_line, name="return_remove_line"),
]
