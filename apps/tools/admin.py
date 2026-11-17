from django.contrib import admin

from .models import ToolDefinition


@admin.register(ToolDefinition)
class ToolDefinitionAdmin(admin.ModelAdmin):
    list_display = ["key", "name", "category", "kind", "mutating", "can_undo", "enabled"]
    list_filter = ["kind", "category", "mutating", "enabled"]
    search_fields = ["key", "name", "description"]
    list_editable = ["enabled"]
