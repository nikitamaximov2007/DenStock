from django.urls import path

from . import views

urlpatterns = [
    path("", views.customer_request_list, name="customer_request_list"),
    path("<int:pk>/", views.customer_request_detail, name="customer_request_detail"),
    path("<int:pk>/status/", views.customer_request_status, name="customer_request_status"),
]
