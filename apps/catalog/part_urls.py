from django.urls import path

from . import part_views

urlpatterns = [
    path("", part_views.PartTypeListView.as_view(), name="part_list"),
    path("new/", part_views.PartTypeCreateView.as_view(), name="part_create"),
    path("<int:pk>/", part_views.PartTypeDetailView.as_view(), name="part_detail"),
    path("<int:pk>/edit/", part_views.PartTypeUpdateView.as_view(), name="part_edit"),
    path("<int:pk>/toggle/", part_views.part_toggle, name="part_toggle"),
    path("<int:pk>/numbers/add/", part_views.number_add, name="part_number_add"),
    path("<int:pk>/barcodes/add/", part_views.barcode_add, name="part_barcode_add"),
    path("<int:pk>/compat/add/", part_views.compat_add, name="part_compat_add"),
    # Аналоги: отдельная деталь со своими остатками, а не второй номер этой же.
    path("<int:pk>/analogs/add/", part_views.analog_add, name="part_analog_add"),
    path("analogs/<int:pk>/unlink/", part_views.analog_unlink, name="part_analog_unlink"),
    path("analogs/<int:pk>/confirm/", part_views.analog_confirm, name="part_analog_confirm"),
    path(
        "analogs/<int:pk>/unconfirm/", part_views.analog_unconfirm, name="part_analog_unconfirm"
    ),
    path("numbers/<int:pk>/delete/", part_views.number_delete, name="part_number_delete"),
    path("barcodes/<int:pk>/delete/", part_views.barcode_delete, name="part_barcode_delete"),
    path("compat/<int:pk>/delete/", part_views.compat_delete, name="part_compat_delete"),
    # Слой 24: фотографии вида детали
    path("<int:pk>/images/add/", part_views.part_image_add, name="part_image_add"),
    path("images/<int:pk>/primary/", part_views.part_image_primary, name="part_image_primary"),
    path("images/<int:pk>/delete/", part_views.part_image_delete, name="part_image_delete"),
    # Публичный каталог: фото попадает туда только явным решением менеджера.
    path("public-photos/", part_views.public_photo_queue, name="public_photo_queue"),
    path(
        "images/<int:pk>/public/publish/",
        part_views.public_photo_publish,
        name="public_photo_publish",
    ),
    path(
        "images/<int:pk>/public/reject/",
        part_views.public_photo_reject,
        name="public_photo_reject",
    ),
    path(
        "public-photos/<int:pk>/primary/",
        part_views.public_photo_primary,
        name="public_photo_primary",
    ),
]
