from django.urls import path

from . import views

app_name = "shops"

urlpatterns = [
    path("stores/", views.store_list, name="list"),
    path("stores/connect/", views.connect_store, name="connect"),
]
