from django.urls import path

from . import internal_views

urlpatterns = [
    path("links/", internal_views.ownership_links, name="customer_account_internal_links"),
]
