from django.urls import path

from . import views

urlpatterns = [
    path("counting/", views.counting_list, name="counting_list"),
    path("counting/new/", views.counting_new, name="counting_new"),
    path("counting/<int:pk>/", views.counting_detail, name="counting_detail"),
    path("counting/<int:pk>/scan/", views.counting_scan, name="counting_scan"),
    path("counting/<int:pk>/undo/", views.counting_undo, name="counting_undo"),
    path("counting/<int:pk>/comment/", views.counting_comment, name="counting_comment"),
    path("counting/<int:pk>/convert/", views.counting_convert, name="counting_convert"),
    path("counting/<int:pk>/post/", views.counting_post, name="counting_post"),
    path("counting/<int:pk>/cancel/", views.counting_cancel, name="counting_cancel"),
    path("counting/<int:pk>/delete/", views.counting_delete, name="counting_delete"),
    path("counting/lines/<int:pk>/qty/", views.counting_line_qty, name="counting_line_qty"),
    path(
        "counting/lines/<int:pk>/remove/",
        views.counting_line_remove,
        name="counting_line_remove",
    ),
    path(
        "counting/lines/<int:pk>/resolve/",
        views.counting_line_resolve,
        name="counting_line_resolve",
    ),
]
