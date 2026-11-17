"""
Signals other apps listen to, so `shopify_app` doesn't need to know about agents,
schedules or billing. Example (later phases):

    @receiver(shop_uninstalled)
    def pause_agents(sender, shop, **kwargs): ...
"""

from django.dispatch import Signal

shop_installed = Signal()  # kwargs: shop, user, is_new_install
shop_uninstalled = Signal()  # kwargs: shop
shop_scopes_updated = Signal()  # kwargs: shop, scopes
customers_redact_requested = Signal()  # kwargs: shop, payload
customers_data_requested = Signal()  # kwargs: shop, payload
shop_redact_requested = Signal()  # kwargs: shop  (sent just before the shop is deleted)
