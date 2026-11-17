from django.contrib import admin

from .models import Conversation, Message


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("role", "content", "run", "created_at")
    readonly_fields = ("role", "content", "run", "created_at")


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ["title", "shop", "user", "installation", "updated_at"]
    search_fields = ["title", "shop__domain"]
    inlines = [MessageInline]
