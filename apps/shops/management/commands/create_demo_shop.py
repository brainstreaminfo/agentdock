"""
Create a pretend store so you can try the whole app WITHOUT a Shopify app:

    python manage.py create_demo_shop --email you@example.com

It creates (or refreshes) the shop `demo-store.myshopify.com`:
* it counts as installed and has granted every permission agents use, so
  installs never send you to Shopify;
* the user with that email becomes its OWNER (created if missing; use your
  staff account to also use it in the Studio playground);
* nothing is ever sent to Shopify for it (`Shop.is_demo`): tools that need
  store data answer with a clear error, while chat, brand memory, charts,
  alerts, reply drafts, schedules and My Team all work.

Remove it when you connect real stores: Django Admin → Shops → delete
"Demo store (no Shopify)". The command refuses to touch a real store.
"""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.accounts.models import Membership, User
from apps.shopify_app import scopes as scope_utils
from apps.shopify_app.hmac import normalize_shop_domain
from apps.shopify_app.topics import AGENT_EVENT_TOPICS
from apps.shops.models import DEMO_PLAN, Shop
from apps.tools.models import ToolDefinition


def all_agent_scopes() -> list[str]:
    """Every scope a tool or an event trigger can need."""
    tool_scopes = [
        s for scopes in ToolDefinition.objects.values_list("scopes", flat=True) for s in scopes
    ]
    topic_scopes = [t["scope"] for t in AGENT_EVENT_TOPICS.values()]
    return scope_utils.union(tool_scopes, topic_scopes, ["read_products"])


class Command(BaseCommand):
    help = "Create a pretend store to try the app without Shopify (demo/testing only)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--email", required=True, help="Becomes the store's OWNER")
        parser.add_argument("--domain", default="demo-store.myshopify.com")
        parser.add_argument("--timezone", default="UTC", help="IANA name, e.g. Asia/Kolkata")
        parser.add_argument("--currency", default="USD")

    @transaction.atomic
    def handle(self, *args: Any, **opts: Any) -> None:
        domain = normalize_shop_domain(opts["domain"])
        if domain is None:
            raise CommandError("Domain must look like name.myshopify.com")
        shop = Shop.objects.filter(domain=domain).first()
        if shop is not None and not shop.is_demo:
            raise CommandError(f"{domain} is a real store; refusing to change it.")
        shop = shop or Shop(domain=domain)
        shop.name = "Demo store (no Shopify)"
        shop.email = opts["email"].lower()
        shop.plan_name = DEMO_PLAN
        shop.is_dev_store = True
        shop.timezone = opts["timezone"]
        shop.currency = opts["currency"]
        shop.granted_scopes = all_agent_scopes()
        shop.installed_at = shop.installed_at or timezone.now()
        shop.uninstalled_at = None
        # A fake, non-expiring token: the app treats the store as connected;
        # real Shopify calls fail with an error the agent can read.
        shop.set_tokens("demo-token-not-valid-on-shopify")
        shop.save()

        user = User.objects.filter(email__iexact=opts["email"]).first()
        if user is None:
            user = User.objects.create_user(email=opts["email"].lower())
            self.stdout.write(
                f"Created user {user.email}. Log in with a magic link, "
                "or set a password in Django Admin."
            )
        Membership.objects.update_or_create(
            user=user, shop=shop, defaults={"role": Membership.Role.OWNER}
        )
        self.stdout.write(
            self.style.SUCCESS(f"Demo store ready: /s/{shop.pk}/  (owner: {user.email})")
        )
