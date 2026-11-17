"""Local development settings: debug on, console email, human-readable logs."""

from .base import *  # noqa: F403
from .base import INSTALLED_APPS, MIDDLEWARE, env

DEBUG = env.bool("DJANGO_DEBUG", default=True)
SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="dev-insecure-secret-key-change-me")
ALLOWED_HOSTS = ["*"]

# Print emails (magic links) in the terminal unless a Resend key is configured.
if not env.str("RESEND_API_KEY", default=""):
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"

LOG_JSON = env.bool("LOG_JSON", default=False)
from apps.core.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(level=env.str("LOG_LEVEL", default="DEBUG"), json=LOG_JSON)

# Plain static storage so `runserver` works without `collectstatic`.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

INSTALLED_APPS = [*INSTALLED_APPS, "debug_toolbar"]
MIDDLEWARE = ["debug_toolbar.middleware.DebugToolbarMiddleware", *MIDDLEWARE]
INTERNAL_IPS = ["127.0.0.1"]

# Start the toolbar collapsed so it does not cover the page.
DEBUG_TOOLBAR_CONFIG = {"SHOW_COLLAPSED": True}
