from django.contrib import admin

from .models import AuditLog


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ["created_at", "action", "shop", "user"]
    list_filter = ["action"]
    search_fields = ["action", "shop__domain", "user__email"]
    readonly_fields = ["created_at", "action", "shop", "user", "meta"]

    def has_add_permission(self, request):  # type: ignore[no-untyped-def]
        return False
