from django.urls import path

from . import views

app_name = "studio"

urlpatterns = [
    path("", views.home, name="home"),
    path("tools/", views.tool_list, name="tools"),
    path("tools/new/", views.tool_create, name="tool_create"),
    path("tools/<str:key>/", views.tool_detail, name="tool_detail"),
    path("tools/<str:key>/edit/", views.tool_edit, name="tool_edit"),
    path("tools/<str:key>/toggle/", views.tool_toggle, name="tool_toggle"),
    path("tools/<str:key>/test/", views.tool_test, name="tool_test"),
]
