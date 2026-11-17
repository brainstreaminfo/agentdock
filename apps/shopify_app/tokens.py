"""
Get a usable Shopify access token for a shop, refreshing it when it is about to expire.

Shopify warns: never refresh the same shop's token twice at the same time (each
refresh retires the other's result). We lock the Shop row (SELECT … FOR UPDATE)
while refreshing, so concurrent Celery workers wait and then reuse the new token.
"""

from __future__ import annotations

from django.conf import settings
from django.db import transaction

from apps.core.audit import audit
from apps.core.logging import get_logger
from apps.shops.models import Shop

from . import oauth

log = get_logger(__name__)


class ShopNotInstalled(Exception):
    """The shop has no token (uninstalled, or needs to re-authorize)."""


def get_access_token(shop: Shop, *, force_refresh: bool = False) -> str:
    if not shop.access_token_enc:
        raise ShopNotInstalled(f"{shop.domain} has no access token (uninstalled or needs OAuth).")

    margin = settings.SHOPIFY_TOKEN_REFRESH_MARGIN_SECONDS
    if not force_refresh and not shop.access_token_needs_refresh(margin):
        return shop.decrypted_access_token()

    try:
        locked = _refresh_locked(shop, force_refresh, margin)
    except oauth.ReauthRequired:
        # Outside the transaction above, so this cleanup isn't rolled back.
        Shop.objects.filter(pk=shop.pk).update(
            access_token_enc="",
            access_token_expires_at=None,
            refresh_token_enc="",
            refresh_token_expires_at=None,
        )
        shop.clear_tokens()
        audit("shop.token_revoked", shop=shop)
        raise

    # Copy the fresh values onto the caller's instance.
    for field in _TOKEN_FIELDS:
        setattr(shop, field, getattr(locked, field))
    return shop.decrypted_access_token()


def _refresh_locked(shop: Shop, force_refresh: bool, margin: int) -> Shop:
    with transaction.atomic():
        locked = Shop.objects.select_for_update().get(pk=shop.pk)
        # Another worker may have refreshed while we waited for the lock.
        if force_refresh or locked.access_token_needs_refresh(margin):
            refresh_token = locked.decrypted_refresh_token()
            if not refresh_token:
                raise ShopNotInstalled(f"{shop.domain} has no refresh token; re-run OAuth.")
            tokens = oauth.refresh_access_token(locked.domain, refresh_token)
            locked.set_tokens(
                tokens.access_token,
                expires_in=tokens.expires_in,
                refresh_token=tokens.refresh_token,
                refresh_token_expires_in=tokens.refresh_token_expires_in,
            )
            locked.save(update_fields=_TOKEN_FIELDS)
            log.info("shop.token_refreshed", shop_id=locked.pk)
    return locked


_TOKEN_FIELDS = [
    "access_token_enc",
    "access_token_expires_at",
    "refresh_token_enc",
    "refresh_token_expires_at",
    "updated_at",
]
