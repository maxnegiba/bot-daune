from django.urls import path
from .views import sign_mandate_view

urlpatterns = [
    path("semneaza/<uuid:case_id>/", sign_mandate_view, name="sign_mandate"),
]
