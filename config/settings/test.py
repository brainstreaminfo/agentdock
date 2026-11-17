"""Settings for pytest. Fast, isolated, never talks to real Shopify / Anthropic / Resend."""

from .base import *  # noqa: F403
from .base import env

SECRET_KEY = "test-secret-key"
DEBUG = False
ALLOWED_HOSTS = ["*"]
APP_URL = "https://app.test"
CSRF_TRUSTED_ORIGINS = [APP_URL]

DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://localhost:5432/agenthub"),
}

SHOPIFY_API_KEY = "test-api-key"
SHOPIFY_API_SECRET = "test-api-secret"
SHOPIFY_API_VERSION = "2026-10"
SHOPIFY_BASE_SCOPES = ["read_products"]
# A valid Fernet key used only in tests.
ENCRYPTION_KEY = "q0V4Wj3r5S2mHqN1oZpXc8Yb6Lk7Jd9Tf0Ga2Rw4Ue8="
RESEND_API_KEY = ""

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Run Celery tasks inline so tests can assert on their effects.
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
CELERY_RESULT_BACKEND = "cache+memory://"

SENTRY_DSN = ""
RUN_EVENTS_BACKEND = "memory"
TRIGGERS_AUTO_SYNC_WEBHOOKS = False  # tests call apps.triggers.subscriptions directly
ANTHROPIC_API_KEY = "test-anthropic-key"
from apps.core.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(level="WARNING", json=False)
