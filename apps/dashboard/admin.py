from django.contrib import admin

from .models import Alert


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = ["title", "shop", "severity", "agent_slug", "created_at", "read_at"]
    list_filter = ["severity"]
    search_fields = ["title", "shop__domain"]
