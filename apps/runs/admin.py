from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import ActionProposal, ActionSnapshot, Run, RunStep, UsageDaily


class RunStepInline(admin.TabularInline):
    model = RunStep
    extra = 0
    fields = ("idx", "type", "name", "duration_ms", "error")
    readonly_fields = ("idx", "type", "name", "duration_ms", "error")
    show_change_link = True


@admin.register(Run)
class RunAdmin(admin.ModelAdmin):
    list_display = ["id", "shop", "agent_slug", "trigger", "status", "cost_usd", "created_at"]
    list_filter = ["status", "trigger"]
    search_fields = ["shop__domain", "agent_slug", "celery_task_id"]
    readonly_fields = [f.name for f in Run._meta.fields]
    inlines = [RunStepInline]


@admin.register(RunStep)
class RunStepAdmin(admin.ModelAdmin):
    list_display = ["run", "idx", "type", "name", "duration_ms"]
    readonly_fields = [f.name for f in RunStep._meta.fields]


class SnapshotInline(admin.StackedInline):
    model = ActionSnapshot
    extra = 0
    readonly_fields = ["before", "after", "created_at"]


@admin.register(ActionProposal)
class ActionProposalAdmin(admin.ModelAdmin):
    list_display = [
        "tool",
        "shop",
        "agent_slug",
        "status",
        "auto_applied",
        "created_at",
        "decided_at",
    ]
    list_filter = ["status", "tool", "auto_applied"]
    search_fields = ["shop__domain", "tool", "uuid"]
    readonly_fields = [f.name for f in ActionProposal._meta.fields]
    inlines = [SnapshotInline]
    actions = ["approve", "undo"]

    @admin.action(description="Approve and apply selected (pending) actions")
    def approve(self, request: HttpRequest, queryset: QuerySet[ActionProposal]) -> None:
        from apps.tools.executor import apply_proposal

        for proposal in queryset.filter(status=ActionProposal.Status.PENDING):
            result = apply_proposal(proposal, user=request.user)
            level = messages.SUCCESS if result.status == "APPLIED" else messages.ERROR
            self.message_user(request, f"{proposal.tool}: {result.status} {result.error}", level)

    @admin.action(description="Undo selected (applied) actions")
    def undo(self, request: HttpRequest, queryset: QuerySet[ActionProposal]) -> None:
        from apps.tools.errors import ToolError
        from apps.tools.executor import undo_proposal

        for proposal in queryset:
            try:
                undo_proposal(proposal, user=request.user)
                self.message_user(request, f"{proposal.tool}: undone")
            except ToolError as exc:
                self.message_user(request, f"{proposal.tool}: {exc.message}", messages.ERROR)


@admin.register(UsageDaily)
class UsageDailyAdmin(admin.ModelAdmin):
    list_display = ["date", "shop", "agent_slug", "runs", "tokens_in", "tokens_out", "cost_usd"]
    list_filter = ["date"]
    search_fields = ["shop__domain", "agent_slug"]
