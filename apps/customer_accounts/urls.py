from django.urls import path

from . import views

urlpatterns = [
    path("", views.home, name="customer_account_home"),
    path("login/", views.login, name="customer_account_login"),
    path("login/max/", views.login_max, name="customer_account_login_max"),
    path("login/code/", views.login_code, name="customer_account_login_code"),
    path("logout/", views.logout, name="customer_account_logout"),
    path("requests/", views.requests_list, name="customer_account_requests"),
    path(
        "requests/<uuid:public_id>/",
        views.request_detail,
        name="customer_account_request",
    ),
    path("purchases/", views.purchases_list, name="customer_account_purchases"),
    path(
        "purchases/<str:number>/",
        views.purchase_detail,
        name="customer_account_purchase",
    ),
    path(
        "purchases/<str:number>/reorder/",
        views.purchase_reorder,
        name="customer_account_reorder",
    ),
    path("messengers/", views.messengers, name="customer_account_messengers"),
    path(
        "messengers/telegram/link/",
        views.telegram_link_start,
        name="customer_account_telegram_link",
    ),
    path(
        "messengers/telegram/code/",
        views.telegram_link_code,
        name="customer_account_telegram_code",
    ),
    path(
        "messengers/telegram/unlink/",
        views.telegram_unlink,
        name="customer_account_telegram_unlink",
    ),
    path("profile/", views.profile, name="customer_account_profile"),
]
