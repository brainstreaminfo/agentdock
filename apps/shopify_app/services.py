"""
Install / sync / uninstall logic, kept out of the views so it is easy to test and
reuse from management commands and Celery tasks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.core.audit import audit
from apps.core.logging import get_logger
from apps.shops.models import Shop

from . import scopes as scope_utils
from . import signals
from .graphql import ShopifyGraphQL
from .oauth import TokenResponse
from .queries import load_query

log = get_logger(__name__)


@dataclass
class InstallResult:
    shop: Shop
    user: User
    is_new_install: bool


def apply_shop_info(shop: Shop, data: dict[str, Any]) -> None:
    """Copy fields from the `ShopInfo` GraphQL query (or a shop/update webhook) onto Shop."""
    info = data.get("shop") or {}
    plan = info.get("plan") or {}
    shop.shopify_gid = info.get("id", shop.shopify_gid) or ""
    shop.name = info.get("name") or shop.name
    shop.email = info.get("email") or shop.email
    shop.currency = info.get("currencyCode") or shop.currency
    shop.timezone = info.get("ianaTimezone") or shop.timezone or "UTC"
    shop.plan_name = plan.get("publicDisplayName") or shop.plan_name
    shop.is_dev_store = bool(plan.get("partnerDevelopment", shop.is_dev_store))
    installation = data.get("currentAppInstallation") or {}
    if "accessScopes" in installation:
        shop.granted_scopes = scope_utils.parse(s["handle"] for s in installation["accessScopes"])
    shop.last_synced_at = timezone.now()


def fetch_shop_info(shop: Shop) -> dict[str, Any]:
    return ShopifyGraphQL(shop).execute(load_query("shop_info")).data


def complete_install(
    *, shop_domain: str, tokens: TokenResponse, logged_in_user: User | None
) -> InstallResult:
    """
    Called by the OAuth callback after a successful code exchange.

    Saves tokens and scopes, fetches shop details, and makes sure a User with an
    OWNER membership exists. If someone is already logged in (e.g. "Connect
    another store"), that person becomes the owner; otherwise we use the shop's
    email address.
    """
    with transaction.atomic():
        shop, created = Shop.objects.select_for_update().get_or_create(domain=shop_domain)
        is_new_install = created or shop.uninstalled_at is not None or shop.installed_at is None
        shop.set_tokens(
            tokens.access_token,
            expires_in=tokens.expires_in,
            refresh_token=tokens.refresh_token,
            refresh_token_expires_in=tokens.refresh_token_expires_in,
        )
        shop.granted_scopes = scope_utils.parse(tokens.scope)
        if is_new_install:
            shop.installed_at = timezone.now()
            shop.uninstalled_at = None
        shop.save()

    # Network call outside the transaction (don't hold locks while waiting on Shopify).
    apply_shop_info(shop, fetch_shop_info(shop))
    shop.save()

    with transaction.atomic():
        user = logged_in_user
        if user is None:
            email = (shop.email or f"owner@{shop.domain}").lower()
            user, _ = User.objects.get_or_create(
                email=email, defaults={"first_name": shop.name[:150]}
            )
            if not user.has_usable_password():
                user.set_unusable_password()
                user.save(update_fields=["password"])
        Membership.objects.get_or_create(
            user=user, shop=shop, defaults={"role": Membership.Role.OWNER}
        )

    audit(
        "shop.installed" if is_new_install else "shop.reauthorized",
        shop=shop,
        user=user,
        scopes=shop.granted_scopes,
    )
    signals.shop_installed.send(sender=Shop, shop=shop, user=user, is_new_install=is_new_install)
    return InstallResult(shop=shop, user=user, is_new_install=is_new_install)


def mark_uninstalled(shop: Shop) -> None:
    """`app/uninstalled`: drop tokens, stop everything, keep data for 30 days."""
    if shop.uninstalled_at is not None:
        return
    shop.clear_tokens()
    shop.granted_scopes = []
    shop.uninstalled_at = timezone.now()
    shop.save()
    audit("shop.uninstalled", shop=shop)
    # Later phases connect to this to pause agents, disable schedules and
    # revoke queued Celery tasks.
    signals.shop_uninstalled.send(sender=Shop, shop=shop)


def purge_shop(shop: Shop, *, reason: str) -> None:
    """Delete a shop and (via CASCADE) all of its data."""
    signals.shop_redact_requested.send(sender=Shop, shop=shop)
    domain = shop.domain
    audit("shop.purged", shop=None, domain=domain, reason=reason)
    shop.delete()
    log.info("shop.purged", domain=domain, reason=reason)


def purge_due_shops() -> int:
    cutoff = timezone.now() - timedelta(days=settings.SHOPIFY_PURGE_AFTER_DAYS)
    due = list(Shop.objects.filter(uninstalled_at__lt=cutoff))
    for shop in due:
        purge_shop(shop, reason=f"uninstalled more than {settings.SHOPIFY_PURGE_AFTER_DAYS} days")
    return len(due)
