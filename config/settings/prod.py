"""Production settings. Fails loudly at startup if a required secret is missing."""

from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403
from .base import ENCRYPTION_KEY, SECRET_KEY, SHOPIFY_API_KEY, SHOPIFY_API_SECRET

DEBUG = False

for _name, _value in {
    "DJANGO_SECRET_KEY": SECRET_KEY,
    "ENCRYPTION_KEY": ENCRYPTION_KEY,
    "SHOPIFY_API_KEY": SHOPIFY_API_KEY,
    "SHOPIFY_API_SECRET": SHOPIFY_API_SECRET,
}.items():
    if not _value:
        raise ImproperlyConfigured(f"Environment variable {_name} must be set in production.")

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
SECURE_SSL_REDIRECT = True
# The platform's health check calls /healthz/ over plain http inside its network.
SECURE_REDIRECT_EXEMPT = [r"^healthz/$"]
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"
