from django.urls import path

from . import views

app_name = "shopify"

urlpatterns = [
    path("launch/", views.launch, name="launch"),
    path("auth/", views.auth, name="auth"),
    path("callback/", views.callback, name="callback"),
    path("webhooks/", views.webhook, name="webhooks"),
]
