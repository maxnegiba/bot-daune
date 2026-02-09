from django.urls import path
from .views import whatsapp_webhook, chat_login, chat_history, chat_send, chat_poll
from .views_admin import (
    admin_chat_dashboard,
    api_get_conversations,
    api_get_messages,
    api_send_message,
)

urlpatterns = [
    # --- Public Client Web Chat & Webhook ---
    path("webhook/", whatsapp_webhook, name="whatsapp_webhook"),
    path("chat/login/", chat_login, name="chat_login"),
    path("chat/history/", chat_history, name="chat_history"),
    path("chat/send/", chat_send, name="chat_send"),
    path("chat/poll/", chat_poll, name="chat_poll"),

    # --- Admin Chat Dashboard ---
    path("admin/dashboard/", admin_chat_dashboard, name="admin_chat_dashboard"),
    path("admin/api/conversations/", api_get_conversations, name="admin_api_conversations"),
    path("admin/api/messages/<uuid:case_id>/", api_get_messages, name="admin_api_messages"),
    path("admin/api/send/<uuid:case_id>/", api_send_message, name="admin_api_send"),
]
