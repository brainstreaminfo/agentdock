"""Re-encrypt stored secrets with the newest ENCRYPTION_KEY (first in the list)."""

from typing import Any

from django.core.management.base import BaseCommand

from apps.core import crypto
from apps.shops.models import Shop


class Command(BaseCommand):
    help = "Re-encrypt Shopify tokens with the first key in ENCRYPTION_KEY."

    def handle(self, *args: Any, **opts: Any) -> None:
        count = 0
        for shop in Shop.objects.exclude(access_token_enc=""):
            shop.access_token_enc = crypto.rotate(shop.access_token_enc)
            if shop.refresh_token_enc:
                shop.refresh_token_enc = crypto.rotate(shop.refresh_token_enc)
            shop.save(update_fields=["access_token_enc", "refresh_token_enc"])
            count += 1
        self.stdout.write(self.style.SUCCESS(f"Re-encrypted tokens for {count} shop(s)."))
