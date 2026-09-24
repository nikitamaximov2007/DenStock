from django.urls import path

from . import views

urlpatterns = [
    path("", views.customer_request_list, name="customer_request_list"),
    path("events/", views.customer_request_events, name="customer_request_events"),
    path("<int:pk>/", views.customer_request_detail, name="customer_request_detail"),
    path("<int:pk>/sale/", views.customer_request_sale, name="customer_request_sale"),
    path(
        "<int:pk>/sale/customs/",
        views.customer_request_customs,
        name="customer_request_customs",
    ),
    path("<int:pk>/status/", views.customer_request_status, name="customer_request_status"),
    path("<int:pk>/delete/", views.customer_request_delete, name="customer_request_delete"),
    path("delete-canceled/", views.customer_request_delete_all, name="customer_request_delete_all"),
    path(
        "<int:pk>/telegram-link/",
        views.customer_request_telegram_link,
        name="customer_request_telegram_link",
    ),
    path(
        "<int:pk>/max-link/",
        views.customer_request_max_link,
        name="customer_request_max_link",
    ),
    path("<int:pk>/reply/", views.customer_request_reply, name="customer_request_reply"),
    path(
        "<int:pk>/max-reply/",
        views.customer_request_max_reply,
        name="customer_request_max_reply",
    ),
    path("telegram/webhook/", views.telegram_webhook, name="customer_request_telegram_webhook"),
    path("max/webhook/", views.max_webhook, name="customer_request_max_webhook"),
    path("telegram/", views.telegram_settings, name="telegram_settings"),
    path(
        "telegram/operators/<int:pk>/toggle/",
        views.telegram_operator_toggle,
        name="telegram_operator_toggle",
    ),
    path(
        "telegram/operators/<int:pk>/role/",
        views.telegram_operator_role,
        name="telegram_operator_role",
    ),
    path("staff-bindings/", views.staff_messenger_bindings, name="staff_messenger_bindings"),
]
