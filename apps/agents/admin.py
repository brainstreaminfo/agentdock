from django.contrib import admin

from .models import ShopMemory


@admin.register(ShopMemory)
class ShopMemoryAdmin(admin.ModelAdmin):
    list_display = ["shop", "key", "source", "updated_at"]
    list_filter = ["source"]
    search_fields = ["shop__domain", "key", "value"]
