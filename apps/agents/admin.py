from django.contrib import admin

from .models import AgentDefinition, AgentInstallation, AgentVersion, ShopMemory


class VersionInline(admin.TabularInline):
    model = AgentVersion
    extra = 0
    fields = ("number", "is_draft", "model", "effort", "published_at", "changelog")
    readonly_fields = ("number", "is_draft", "model", "effort", "published_at", "changelog")
    show_change_link = True


@admin.register(AgentDefinition)
class AgentDefinitionAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "category", "status", "visibility", "is_template", "featured"]
    list_filter = ["status", "visibility", "is_template"]
    search_fields = ["name", "slug", "tagline"]
    prepopulated_fields = {"slug": ["name"]}
    inlines = [VersionInline]


@admin.register(AgentVersion)
class AgentVersionAdmin(admin.ModelAdmin):
    list_display = ["agent", "number", "is_draft", "model", "effort", "published_at"]
    list_filter = ["is_draft", "model"]
    filter_horizontal = ["tools"]


@admin.register(AgentInstallation)
class AgentInstallationAdmin(admin.ModelAdmin):
    list_display = ["agent", "shop", "version", "autonomy", "enabled", "last_run_at"]
    list_filter = ["autonomy", "enabled"]
    search_fields = ["shop__domain", "agent__slug"]


@admin.register(ShopMemory)
class ShopMemoryAdmin(admin.ModelAdmin):
    list_display = ["shop", "key", "source", "updated_at"]
    list_filter = ["source"]
    search_fields = ["shop__domain", "key", "value"]
