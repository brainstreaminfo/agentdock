from django.urls import path

from . import views

app_name = "marketplace"

urlpatterns = [
    path("marketplace/", views.home, name="home"),
    path("marketplace/<slug:slug>/", views.detail, name="detail"),
    path("marketplace/<slug:slug>/install/", views.install, name="install"),
    path("marketplace/<slug:slug>/grant/", views.grant, name="grant"),
    path("agents/", views.my_agents, name="agents"),
    path("agents/<int:installation_id>/", views.agent_page, name="agent"),
    path("agents/<int:installation_id>/run/", views.run_now, name="run_now"),
    path("agents/<int:installation_id>/toggle/", views.toggle, name="toggle"),
    path("agents/<int:installation_id>/update/", views.update, name="update"),
    path("agents/<int:installation_id>/uninstall/", views.uninstall, name="uninstall"),
    path("pickers/<str:kind>/", views.picker, name="picker"),
]
