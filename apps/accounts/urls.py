from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("login/<str:token>/", views.magic_link_view, name="magic"),
    path("logout/", views.logout_view, name="logout"),
]
