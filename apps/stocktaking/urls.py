from django.urls import path

from . import section_views, views

urlpatterns = [
    path("section/", section_views.section_recount_list, name="section_recount_list"),
    path("section/new/", section_views.section_recount_new, name="section_recount_new"),
    path(
        "section/<int:pk>/",
        section_views.section_recount_detail,
        name="section_recount_detail",
    ),
    path(
        "section/<int:pk>/start/",
        section_views.section_recount_start,
        name="section_recount_start",
    ),
    path(
        "section/<int:pk>/scan/",
        section_views.section_recount_scan,
        name="section_recount_scan",
    ),
    path(
        "section/<int:pk>/cells/<int:cell_number>/complete/",
        section_views.section_recount_complete_cell,
        name="section_recount_complete_cell",
    ),
    path(
        "section/<int:pk>/lines/<int:line_pk>/quantity/",
        section_views.section_recount_set_quantity,
        name="section_recount_set_quantity",
    ),
    path(
        "section/<int:pk>/lines/<int:line_pk>/remove/",
        section_views.section_recount_remove_line,
        name="section_recount_remove_line",
    ),
    path(
        "section/<int:pk>/lines/<int:line_pk>/allocate/",
        section_views.section_recount_allocate,
        name="section_recount_allocate",
    ),
    path(
        "section/<int:pk>/ready/",
        section_views.section_recount_ready,
        name="section_recount_ready",
    ),
    path(
        "section/<int:pk>/apply/",
        section_views.section_recount_apply_view,
        name="section_recount_apply",
    ),
    path(
        "section/<int:pk>/cancel/",
        section_views.section_recount_cancel,
        name="section_recount_cancel",
    ),
    path("", views.inventory_count_list, name="inventory_count_list"),
    path("new/", views.inventory_count_create, name="inventory_count_create"),
    path("initial/<int:pk>/", views.initial_inventory_detail, name="initial_inventory_detail"),
    path("<int:pk>/", views.inventory_count_detail, name="inventory_count_detail"),
    path("<int:pk>/add-lot/", views.inventory_count_add_lot, name="inventory_count_add_lot"),
    path("<int:pk>/complete/", views.inventory_count_complete, name="inventory_count_complete"),
    path("<int:pk>/cancel/", views.inventory_count_cancel, name="inventory_count_cancel"),
    path(
        "lines/<int:pk>/count/",
        views.inventory_count_set_count, name="inventory_count_set_count",
    ),
    path(
        "lines/<int:pk>/remove/",
        views.inventory_count_remove_line, name="inventory_count_remove_line",
    ),
]
