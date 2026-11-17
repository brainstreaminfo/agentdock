from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from apps.accounts.models import Membership

from .models import Shop


class MembershipInline(admin.TabularInline):
    model = Membership
    extra = 0
    autocomplete_fields = ["user"]


@admin.register(Shop)
class ShopAdmin(admin.ModelAdmin):
    list_display = ["domain", "name", "plan_name", "is_dev_store", "installed_at", "uninstalled_at"]
    list_filter = ["is_dev_store", "uninstalled_at"]
    search_fields = ["domain", "name", "email"]
    inlines = [MembershipInline]
    # Tokens are excluded on purpose: they must never be shown, even to staff.
    exclude = ["access_token_enc", "refresh_token_enc"]
    readonly_fields = [
        "access_token_expires_at",
        "refresh_token_expires_at",
        "installed_at",
        "uninstalled_at",
        "last_synced_at",
        "created_at",
        "updated_at",
    ]
    actions = ["resync"]

    @admin.action(description="Re-sync shop details and scopes from Shopify")
    def resync(self, request: HttpRequest, queryset: QuerySet[Shop]) -> None:
        from apps.shopify_app.tasks import sync_shop

        for shop in queryset:
            sync_shop.delay(shop.pk)
        messages.success(request, f"Queued re-sync for {queryset.count()} shop(s).")
