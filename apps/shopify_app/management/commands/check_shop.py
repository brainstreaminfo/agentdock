"""
Diagnose a shop's connection:  python manage.py check_shop example.myshopify.com

Prints: install status, token expiry, granted vs base scopes, and (with a live
call to Shopify) shop info and registered webhooks. Never prints the token itself.
"""

from __future__ import annotations

from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.shopify_app.graphql import ShopifyGraphQL, ShopifyGraphQLError
from apps.shopify_app.hmac import normalize_shop_domain
from apps.shopify_app.oauth import OAuthError
from apps.shopify_app.queries import load_query
from apps.shopify_app.tokens import ShopNotInstalled
from apps.shops.models import Shop


class Command(BaseCommand):
    help = "Check a shop's token, scopes and webhooks."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("domain")
        parser.add_argument("--offline", action="store_true", help="Skip calls to Shopify")

    def handle(self, *args: Any, **opts: Any) -> None:
        domain = normalize_shop_domain(opts["domain"])
        if domain is None:
            raise CommandError("Pass a domain like example.myshopify.com")
        shop = Shop.objects.filter(domain=domain).first()
        if shop is None:
            raise CommandError(f"No shop {domain} in the database.")

        ok, bad = self.style.SUCCESS("✓"), self.style.ERROR("✗")
        now = timezone.now()
        self.stdout.write(self.style.MIGRATE_HEADING(f"{shop} ({shop.domain}) — id {shop.pk}"))
        self.stdout.write(
            f"  {ok if shop.is_installed else bad} installed "
            f"(installed_at={shop.installed_at}, uninstalled_at={shop.uninstalled_at})"
        )
        if shop.access_token_expires_at:
            left = shop.access_token_expires_at - now
            self.stdout.write(f"  · access token expires in {left} (auto-refreshed when used)")
        if shop.refresh_token_expires_at:
            left = shop.refresh_token_expires_at - now
            mark = ok if left.days > 14 else bad
            self.stdout.write(f"  {mark} refresh token expires in {left.days} days")
        missing = shop.missing_scopes(settings.SHOPIFY_BASE_SCOPES)
        self.stdout.write(
            f"  {bad if missing else ok} scopes: {', '.join(shop.granted_scopes) or '—'}"
            + (f"  (missing base: {', '.join(missing)})" if missing else "")
        )

        if opts["offline"] or not shop.is_installed:
            return
        client = ShopifyGraphQL(shop)
        try:
            info = client.execute(load_query("shop_info")).data
            live = sorted(s["handle"] for s in info["currentAppInstallation"]["accessScopes"])
            self.stdout.write(f"  {ok} API call works — Shopify name: {info['shop']['name']}")
            if live != sorted(shop.granted_scopes):
                self.stdout.write(
                    f"  {bad} DB scopes differ from Shopify: {', '.join(live)} "
                    "(run: sync from Django Admin → Shops → Re-sync)"
                )
            hooks = client.execute(load_query("webhook_subscriptions")).data
            nodes = hooks["webhookSubscriptions"]["nodes"]
            self.stdout.write(
                f"  · {len(nodes)} API-registered webhook(s) "
                "(app/uninstalled + compliance come from shopify.app.toml)"
            )
            for node in nodes:
                self.stdout.write(f"      {node['topic']:<30} {node.get('uri', '')}")
        except (ShopifyGraphQLError, ShopNotInstalled, OAuthError) as exc:
            self.stdout.write(f"  {bad} Shopify call failed: {exc}")
