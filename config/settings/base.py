"""
Base settings shared by every environment.

Every value that changes between environments comes from an environment variable
(see `.env.example`). Never put secrets in this file.
"""

from __future__ import annotations

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env()
# Load `.env` if present (local development). In production the platform
# (Railway / Render / Fly) injects real environment variables instead.
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)

# --------------------------------------------------------------------------- app
APP_NAME = env.str("APP_NAME", default="AgentHub")
APP_URL = env.str("APP_URL", default="http://localhost:8000").rstrip("/")

SECRET_KEY = env.str("DJANGO_SECRET_KEY", default="")
DEBUG = env.bool("DJANGO_DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
CSRF_TRUSTED_ORIGINS = [APP_URL]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "django_celery_beat",
    # Our apps — order matters only for template overrides.
    "apps.core",
    "apps.accounts",
    "apps.shops",
    "apps.shopify_app",
    "apps.dashboard",
    "apps.agents",
    "apps.runs",
    "apps.tools",
    "apps.studio",
    "apps.chat",
    "apps.billing",
    "apps.triggers",
    "apps.marketplace",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    # Adds request_id / user_id / shop_id to every log line of a request.
    "apps.core.middleware.RequestLogContextMiddleware",
]

ROOT_URLCONF = "config.urls"
ASGI_APPLICATION = "config.asgi.application"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.app",
                "apps.shops.context_processors.current_shop",
            ],
        },
    },
]

# ---------------------------------------------------------------------- database
DATABASES = {
    "default": env.db("DATABASE_URL", default="postgres://localhost:5432/agenthub"),
}
DATABASES["default"]["CONN_MAX_AGE"] = env.int("DB_CONN_MAX_AGE", default=60)
DATABASES["default"]["CONN_HEALTH_CHECKS"] = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# -------------------------------------------------------------------------- auth
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "shops:list"
LOGOUT_REDIRECT_URL = "accounts:login"
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]
MAGIC_LINK_MAX_AGE_SECONDS = 15 * 60

# ----------------------------------------------------------------- sessions/csrf
# Non-embedded app: our pages are never inside Shopify's iframe, so normal
# first-party cookies with SameSite=Lax work (the OAuth callback is a top-level
# GET navigation, which Lax cookies are sent on).
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_AGE = 60 * 60 * 24 * 14
X_FRAME_OPTIONS = "DENY"

# ------------------------------------------------------------------------- i18n
LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

# ------------------------------------------------------------------ static files
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

# ------------------------------------------------------------------ cache/redis
REDIS_URL = env.str("REDIS_URL", default="redis://localhost:6379/0")
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
    }
}

# ------------------------------------------------------------------------ celery
CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_TASK_SERIALIZER = "json"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TIMEZONE = "UTC"
CELERY_TASK_ACKS_LATE = True
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_TASK_TIME_LIMIT = 15 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 14 * 60
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_WORKER_HIJACK_ROOT_LOGGER = False
# Platform maintenance jobs. django-celery-beat copies these into the database on
# start-up; per-agent schedules (Phase 5) are created directly in the database.
CELERY_BEAT_SCHEDULE = {
    "purge-uninstalled-shops": {
        "task": "apps.shopify_app.tasks.purge_uninstalled_shops",
        "schedule": 60 * 60 * 24,
    },
    "refresh-dormant-tokens": {
        "task": "apps.shopify_app.tasks.refresh_dormant_tokens",
        "schedule": 60 * 60 * 24,
    },
    "delete-old-webhook-events": {
        "task": "apps.shopify_app.tasks.delete_old_webhook_events",
        "schedule": 60 * 60 * 24,
    },
}

# ----------------------------------------------------------------------- shopify
SHOPIFY_API_KEY = env.str("SHOPIFY_API_KEY", default="")
SHOPIFY_API_SECRET = env.str("SHOPIFY_API_SECRET", default="")
SHOPIFY_API_VERSION = env.str("SHOPIFY_API_VERSION", default="2026-10")
SHOPIFY_BASE_SCOPES = env.list("SHOPIFY_BASE_SCOPES", default=["read_products"])
# How old (seconds) a launch link from Shopify Admin may be before we reject it.
SHOPIFY_LAUNCH_MAX_AGE_SECONDS = env.int("SHOPIFY_LAUNCH_MAX_AGE_SECONDS", default=300)
# Refresh the expiring offline token when it has less than this many seconds left.
SHOPIFY_TOKEN_REFRESH_MARGIN_SECONDS = 5 * 60
# Days after `app/uninstalled` before we delete the shop's data.
SHOPIFY_PURGE_AFTER_DAYS = 30

# ---------------------------------------------------------------------- security
ENCRYPTION_KEY = env.str("ENCRYPTION_KEY", default="")

# --------------------------------------------------------------------------- LLM
ANTHROPIC_API_KEY = env.str("ANTHROPIC_API_KEY", default="")
ALLOWED_MODELS = env.list(
    "ALLOWED_MODELS", default=["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
)
DEFAULT_MODEL = env.str("DEFAULT_MODEL", default="claude-opus-5-5")
# Max output tokens of ONE model turn (a run has several turns; the per-run total
# is AgentVersion.max_tokens). Streaming is used, so large values are fine.
LLM_MAX_TOKENS_PER_TURN = env.int("LLM_MAX_TOKENS_PER_TURN", default=32_000)
# Per-shop spend cap per day (USD). Phase 6 replaces this with plan limits.
# Re-sync each shop's Shopify event subscriptions when its agents change (apps/triggers).
TRIGGERS_AUTO_SYNC_WEBHOOKS = env.bool("TRIGGERS_AUTO_SYNC_WEBHOOKS", default=True)
DEFAULT_DAILY_SPEND_LIMIT_USD = env.float("DEFAULT_DAILY_SPEND_LIMIT_USD", default=10.0)
# Models that support server-side refusal fallbacks (fallbacks: "default").
LLM_FALLBACK_MODELS = [
    "claude-fable-5-2",
    "claude-fable-5-1",
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-sonnet-5-5",
]
# USD per 1M tokens: input, output, cache write (5-min), cache read. Update when
# Anthropic changes prices; unknown models are logged and costed at 0.
MODEL_PRICING = {
    "claude-fable-5-2": {"input": 8.0, "output": 40.0, "cache_write": 10.0, "cache_read": 0.20},
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_write": 5.0, "cache_read": 0.20},
    "claude-opus-5": {"input": 5.0, "output": 25.0, "cache_write": 6.25, "cache_read": 0.50},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_write": 2.5, "cache_read": 0.10},
    "claude-haiku-5-5": {"input": 0.10, "output": 0.50, "cache_write": 0.125, "cache_read": 0.01},
}
# Where live run events (streamed text, tool calls) go: "redis" or "memory" (tests).
RUN_EVENTS_BACKEND = env.str("RUN_EVENTS_BACKEND", default="redis")

# ------------------------------------------------------------------------- email
RESEND_API_KEY = env.str("RESEND_API_KEY", default="")
DEFAULT_FROM_EMAIL = env.str("DEFAULT_FROM_EMAIL", default=f"{APP_NAME} <login@example.com>")
EMAIL_BACKEND = "apps.core.email.ResendEmailBackend"

# ----------------------------------------------------------------------- logging
LOG_LEVEL = env.str("LOG_LEVEL", default="INFO")
LOG_JSON = env.bool("LOG_JSON", default=True)

from apps.core.logging import build_logging_config  # noqa: E402

LOGGING = build_logging_config(level=LOG_LEVEL, json=LOG_JSON)

# ------------------------------------------------------------------------ sentry
SENTRY_DSN = env.str("SENTRY_DSN", default="")
SENTRY_ENVIRONMENT = env.str("SENTRY_ENVIRONMENT", default="development")
