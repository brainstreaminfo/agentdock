"""Users (log in by email), shop memberships with roles, and magic-link tokens."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Any, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone

from apps.core.models import TimeStampedModel


class UserManager(BaseUserManager["User"]):
    use_in_migrations = True

    def _create_user(self, email: str, password: str | None, **extra: Any) -> User:
        if not email:
            raise ValueError("Email is required")
        user = self.model(email=self.normalize_email(email).lower(), **extra)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()  # merchants log in by magic link / Shopify launch
        user.save(using=self._db)
        return user

    def create_user(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email: str, password: str | None = None, **extra: Any) -> User:
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """
    A person. Merchants are created automatically at install (from the shop's
    email). `is_staff=True` users are the platform owner/team and can use the
    Agent Studio and Django Admin.
    """

    username = None  # type: ignore[assignment]
    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS: ClassVar[list[str]] = []

    objects: ClassVar[UserManager] = UserManager()  # type: ignore[assignment]

    def __str__(self) -> str:
        return self.email


class Membership(TimeStampedModel):
    """Links a user to a shop with a role. A user can belong to many shops."""

    class Role(models.TextChoices):
        OWNER = "OWNER", "Owner"
        ADMIN = "ADMIN", "Admin"
        MEMBER = "MEMBER", "Member"

    # Higher number = more power. Used by `has_role`.
    ROLE_RANK: ClassVar[dict[str, int]] = {Role.MEMBER: 1, Role.ADMIN: 2, Role.OWNER: 3}

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    shop = models.ForeignKey("shops.Shop", on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=10, choices=Role.choices, default=Role.MEMBER)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["user", "shop"], name="unique_membership")]

    def __str__(self) -> str:
        return f"{self.user} @ {self.shop} ({self.role})"

    def has_role(self, minimum: str) -> bool:
        return self.ROLE_RANK[self.role] >= self.ROLE_RANK[minimum]


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


class MagicLinkTokenManager(models.Manager["MagicLinkToken"]):
    def issue(self, user: User) -> str:
        """Create a token for `user` and return the RAW value (only ever emailed)."""
        raw = secrets.token_urlsafe(32)
        self.create(
            user=user,
            token_hash=_hash(raw),
            expires_at=timezone.now() + timedelta(seconds=settings.MAGIC_LINK_MAX_AGE_SECONDS),
        )
        return raw

    def consume(self, raw: str) -> User | None:
        """Return the user if the token is valid, unused and unexpired; mark it used."""
        token = (
            self.select_related("user")
            .filter(token_hash=_hash(raw), used_at__isnull=True, expires_at__gt=timezone.now())
            .first()
        )
        if token is None or not token.user.is_active:
            return None
        # Atomic "mark used" so the same link can't be used twice in a race.
        updated = self.filter(pk=token.pk, used_at__isnull=True).update(used_at=timezone.now())
        return token.user if updated else None


class MagicLinkToken(models.Model):
    """Single-use, 15-minute login link. Only a SHA-256 hash is stored."""

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="magic_links")
    token_hash = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used_at = models.DateTimeField(null=True, blank=True)

    objects = MagicLinkTokenManager()

    def __str__(self) -> str:
        return f"Magic link for {self.user} (expires {self.expires_at:%H:%M})"
