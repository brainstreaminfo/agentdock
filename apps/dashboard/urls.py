from django.urls import path

from . import views

app_name = "dashboard"

urlpatterns = [
    path("", views.home, name="home"),
    path("welcome/", views.onboarding, name="onboarding"),
    path("settings/", views.settings_view, name="settings"),
    path("alerts/", views.alerts, name="alerts"),
    path("alerts/read/", views.alert_read, name="alerts_read_all"),
    path("alerts/<int:alert_id>/read/", views.alert_read, name="alert_read"),
    path("memory/", views.memory, name="memory"),
    path("memory/<int:memory_id>/delete/", views.memory_delete, name="memory_delete"),
]
