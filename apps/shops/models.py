"""The Shop: one Shopify store that installed our app."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.db import models
from django.utils import timezone

from apps.core import crypto
from apps.core.models import TimeStampedModel
from apps.shopify_app import scopes as scope_utils

# `plan_name` of pretend stores made by `manage.py create_demo_shop` (no Shopify).
DEMO_PLAN = "Demo (no Shopify)"


class ShopQuerySet(models.QuerySet["Shop"]):
    def installed(self) -> ShopQuerySet:
        return self.filter(uninstalled_at__isnull=True)


class Shop(TimeStampedModel):
    """
    A Shopify store.

    Tokens: Shopify issues *expiring* offline access tokens (1 hour) with a
    refresh token (90 days). Both are Fernet-encrypted in the `*_enc` columns and
    must never be rendered or logged. Code that calls Shopify gets a valid token
    via `apps.shopify_app.tokens.get_access_token(shop)`, which refreshes when needed.
    """

    domain = models.CharField(max_length=255, unique=True, help_text="example.myshopify.com")
    name = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    shopify_gid = models.CharField(max_length=100, blank=True)
    plan_name = models.CharField(max_length=100, blank=True, help_text="Shopify plan, e.g. Basic")
    is_dev_store = models.BooleanField(
        default=False, help_text="Development/partner test store (billing uses test charges)."
    )
    currency = models.CharField(max_length=3, blank=True)
    timezone = models.CharField(max_length=64, default="UTC", help_text="IANA, e.g. Europe/London")

    access_token_enc = models.TextField(blank=True, editable=False)
    access_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)
    refresh_token_enc = models.TextField(blank=True, editable=False)
    refresh_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)

    granted_scopes = models.JSONField(default=list, blank=True)
    installed_at = models.DateTimeField(null=True, blank=True)
    uninstalled_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    objects = ShopQuerySet.as_manager()

    class Meta:
        ordering = ["name", "domain"]

    def __str__(self) -> str:
        return self.name or self.domain

    # ------------------------------------------------------------- status
    @property
    def is_installed(self) -> bool:
        return self.uninstalled_at is None and bool(self.access_token_enc)

    @property
    def is_demo(self) -> bool:
        """A pretend store for testing without Shopify: no Shopify call is ever made."""
        return self.plan_name == DEMO_PLAN

    @property
    def admin_url(self) -> str:
        return f"https://{self.domain}/admin"

    # ------------------------------------------------------------- tokens
    def set_tokens(
        self,
        access_token: str,
        *,
        expires_in: int | None = None,
        refresh_token: str | None = None,
        refresh_token_expires_in: int | None = None,
    ) -> None:
        """Encrypt and store tokens from an OAuth or refresh response (does not save)."""
        now = timezone.now()
        self.access_token_enc = crypto.encrypt(access_token)
        self.access_token_expires_at = now + timedelta(seconds=expires_in) if expires_in else None
        if refresh_token:
            self.refresh_token_enc = crypto.encrypt(refresh_token)
            self.refresh_token_expires_at = (
                now + timedelta(seconds=refresh_token_expires_in)
                if refresh_token_expires_in
                else None
            )

    def clear_tokens(self) -> None:
        self.access_token_enc = ""
        self.access_token_expires_at = None
        self.refresh_token_enc = ""
        self.refresh_token_expires_at = None

    def decrypted_access_token(self) -> str:
        """Raw token. Prefer `tokens.get_access_token(shop)`, which refreshes first."""
        return crypto.decrypt(self.access_token_enc) if self.access_token_enc else ""

    def decrypted_refresh_token(self) -> str:
        return crypto.decrypt(self.refresh_token_enc) if self.refresh_token_enc else ""

    def access_token_needs_refresh(self, margin_seconds: int, now: datetime | None = None) -> bool:
        if self.access_token_expires_at is None:  # legacy non-expiring token
            return False
        now = now or timezone.now()
        return self.access_token_expires_at - now <= timedelta(seconds=margin_seconds)

    # ------------------------------------------------------------- scopes
    def has_scopes(self, required: list[str] | set[str]) -> bool:
        return scope_utils.satisfies(required, self.granted_scopes)

    def missing_scopes(self, required: list[str] | set[str]) -> list[str]:
        return scope_utils.missing(required, self.granted_scopes)
