"""
DEVELOPMENT helper until the Marketplace install flow exists (Phase 5):

    python manage.py install_agent --shop demo.myshopify.com --agent sales-qa-analyst --publish

Installs the agent's latest published version in the shop (--publish first
publishes the current draft). Does NOT check or request Shopify scopes, so tools
needing scopes the shop lacks will answer "permission needed".
"""

from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.agents.models import AgentDefinition, AgentInstallation, Autonomy
from apps.shopify_app.hmac import normalize_shop_domain
from apps.shops.models import Shop


class Command(BaseCommand):
    help = "Install an agent in a shop (development helper)."

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--shop", required=True)
        parser.add_argument("--agent", required=True)
        parser.add_argument("--publish", action="store_true", help="Publish the draft first")
        parser.add_argument("--autonomy", choices=list(Autonomy.values))

    def handle(self, *args: Any, **opts: Any) -> None:
        domain = normalize_shop_domain(opts["shop"])
        shop = Shop.objects.filter(domain=domain).first() if domain else None
        agent = AgentDefinition.objects.filter(slug=opts["agent"]).first()
        if shop is None or agent is None:
            raise CommandError("Unknown shop or agent.")
        if opts["publish"]:
            published = agent.draft.publish(changelog="Published by install_agent")
            self.stdout.write(f"Published {agent.slug} {published.label}")
        version = agent.latest_version
        if version is None:
            raise CommandError(f"{agent.slug} has no published version; use --publish.")
        installation, created = AgentInstallation.objects.update_or_create(
            shop=shop,
            agent=agent,
            defaults={
                "version": version,
                "autonomy": opts["autonomy"] or version.default_autonomy,
                "settings": dict(version.settings_defaults or {}),
                "enabled": True,
            },
        )
        missing = shop.missing_scopes(version.required_scopes)
        self.stdout.write(
            self.style.SUCCESS(
                f"{'Installed' if created else 'Updated'} {agent.name} {version.label} "
                f"in {shop} (autonomy {installation.autonomy})."
            )
        )
        if missing:
            self.stdout.write(self.style.WARNING("Shop is missing scopes: " + ", ".join(missing)))
