from django.urls import path
from .views import whatsapp_webhook, chat_login, chat_history, chat_send, chat_poll

urlpatterns = [
    path("webhook/", whatsapp_webhook, name="whatsapp_webhook"),
    path("chat/login/", chat_login, name="chat_login"),
    path("chat/history/", chat_history, name="chat_history"),
    path("chat/send/", chat_send, name="chat_send"),
    path("chat/poll/", chat_poll, name="chat_poll"),
]
