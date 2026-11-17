"""Top-level URL routes. Each app owns its own `urls.py`."""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path

from apps.core import views as core_views

urlpatterns = [
    path("", core_views.home, name="home"),
    path("healthz/", core_views.healthz, name="healthz"),
    path("admin/", admin.site.urls),
    path("", include("apps.accounts.urls")),
    path("", include("apps.shops.urls")),
    path("shopify/", include("apps.shopify_app.urls")),
    path("studio/", include("apps.studio.urls")),
    path("s/<int:shop_id>/", include("apps.dashboard.urls")),
]

if settings.DEBUG and "debug_toolbar" in settings.INSTALLED_APPS:
    urlpatterns = [path("__debug__/", include("debug_toolbar.urls")), *urlpatterns]
