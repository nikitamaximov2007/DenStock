from django.urls import path

from . import views

app_name = "ai_support"

urlpatterns = [
    path("", views.support_home, name="home"),
    path("conversations/new/", views.conversation_create, name="conversation_create"),
    path(
        "conversations/<uuid:conversation_id>/",
        views.conversation_detail,
        name="conversation",
    ),
    path(
        "conversations/<uuid:conversation_id>/messages/",
        views.message_send,
        name="message_send",
    ),
    path("messages/<uuid:message_id>/rating/", views.message_rating, name="message_rating"),
    path(
        "conversations/<uuid:conversation_id>/tickets/",
        views.ticket_create,
        name="ticket_create",
    ),
    path(
        "attachments/<uuid:attachment_id>/",
        views.attachment_download,
        name="attachment",
    ),
    path("tickets/", views.ticket_list, name="ticket_list"),
    path("tickets/<uuid:ticket_id>/", views.ticket_detail, name="ticket_detail"),
    path(
        "tickets/<uuid:ticket_id>/status/",
        views.ticket_status,
        name="ticket_status",
    ),
]
