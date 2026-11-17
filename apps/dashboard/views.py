"""
Merchant dashboard, alerts, brand memory and settings (spec §8.5).

    /s/<shop>/                 KPIs, pending approvals, alerts, recent runs, agents
    /s/<shop>/alerts/          alerts from agents and the platform (mark read)
    /s/<shop>/memory/          brand memory shared by all agents (key → text)
    /s/<shop>/settings/        store, permissions, team
"""

from __future__ import annotations

from decimal import Decimal

from django import forms
from django.contrib import messages
from django.core.paginator import Paginator
from django.db.models import Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from apps.agents.models import AgentInstallation, ShopMemory
from apps.core.audit import audit
from apps.core.decorators import require_shop_access
from apps.runs.models import ActionProposal, UsageDaily
from apps.runs.views import merchant_runs
from apps.shopify_app import scopes as scope_utils
from apps.triggers.schedules import next_run_at

from .models import Alert


@require_shop_access()
def home(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    month_start = timezone.localdate().replace(day=1)
    installs = list(
        AgentInstallation.objects.for_shop(shop).select_related("agent", "version", "shop")
    )
    pending = ActionProposal.objects.for_shop(shop).filter(status=ActionProposal.Status.PENDING)
    open_alerts = Alert.objects.for_shop(shop).filter(read_at__isnull=True)
    month_usage = UsageDaily.objects.filter(shop=shop, date__gte=month_start).aggregate(
        runs=Sum("runs"), cost=Sum("cost_usd")
    )
    return render(
        request,
        "dashboard/home.html",
        {
            "kpis": {
                "agents": len(installs),
                "runs_month": month_usage["runs"] or 0,
                "pending": pending.count(),
                "alerts": open_alerts.count(),
                "cost_hint": f"AI cost this month: ${month_usage['cost'] or Decimal(0):.2f}",
            },
            "pending": pending.select_related("snapshot").order_by("created_at")[:5],
            "alerts": open_alerts[:5],
            "runs": merchant_runs(shop).select_related("version")[:8],
            "installs": [{"inst": i, "next_run": next_run_at(i)} for i in installs],
        },
    )


@require_shop_access()
def onboarding(request: HttpRequest, shop_id: int) -> HttpResponse:
    return render(request, "dashboard/onboarding.html")


@require_shop_access()
def settings_view(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    permissions = [
        {"scope": s, "label": scope_utils.describe(s)} for s in sorted(shop.granted_scopes)
    ]
    members = shop.memberships.select_related("user").order_by("created_at")
    return render(
        request, "dashboard/settings.html", {"permissions": permissions, "members": members}
    )


# --------------------------------------------------------------------------- alerts
@require_shop_access()
def alerts(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    show = request.GET.get("show", "unread")
    qs = Alert.objects.for_shop(shop)
    if show != "all":
        qs = qs.filter(read_at__isnull=True)
    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    return render(request, "dashboard/alerts.html", {"page": page, "show": show})


@require_shop_access()
@require_POST
def alert_read(request: HttpRequest, shop_id: int, alert_id: int | None = None) -> HttpResponse:
    """Mark one alert (or all, without an id) as read."""
    qs = Alert.objects.for_shop(request.shop).filter(read_at__isnull=True)  # type: ignore[attr-defined]
    if alert_id is not None:
        qs = qs.filter(pk=alert_id)
    qs.update(read_at=timezone.now())
    return redirect("dashboard:alerts", shop_id=shop_id)


# --------------------------------------------------------------------------- brand memory
MEMORY_SUGGESTIONS = {
    "brand_voice": "How your brand sounds, e.g. warm, playful, no slang.",
    "audience": "Who your customers are.",
    "policies": "Shipping, returns and other rules agents should respect.",
    "banned_words": "Words or claims agents must never use.",
}


class MemoryForm(forms.Form):
    key = forms.SlugField(
        max_length=100,
        label="Name",
        help_text="Lowercase, e.g. brand_voice. Agents can use it as {{memory.brand_voice}}.",
        widget=forms.TextInput(attrs={"class": "input", "placeholder": "brand_voice"}),
    )
    value = forms.CharField(
        max_length=5000, widget=forms.Textarea(attrs={"class": "input", "rows": 3})
    )

    def clean_key(self) -> str:
        return str(self.cleaned_data["key"]).lower().replace("-", "_")


@require_shop_access()
def memory(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    can_edit = request.membership.has_role("ADMIN")  # type: ignore[attr-defined]
    form = MemoryForm(request.POST or None, initial={"key": request.GET.get("key", "")})
    if request.method == "POST":
        if not can_edit:
            messages.error(request, "Only store admins can change brand memory.")
            return redirect("dashboard:memory", shop_id=shop_id)
        if form.is_valid():
            entry, created = ShopMemory.objects.update_or_create(
                shop=shop,
                key=form.cleaned_data["key"],
                defaults={
                    "value": form.cleaned_data["value"],
                    "source": ShopMemory.Source.MERCHANT,
                },
            )
            audit("memory.saved", shop=shop, user=request.user, key=entry.key, created=created)
            messages.success(request, f"Saved “{entry.key}”.")
            return redirect("dashboard:memory", shop_id=shop_id)
    entries = list(ShopMemory.objects.for_shop(shop))
    used = {e.key for e in entries}
    return render(
        request,
        "dashboard/memory.html",
        {
            "entries": entries,
            "form": form,
            "can_edit": can_edit,
            "suggestions": {k: v for k, v in MEMORY_SUGGESTIONS.items() if k not in used},
        },
    )


@require_shop_access(role="ADMIN")
@require_POST
def memory_delete(request: HttpRequest, shop_id: int, memory_id: int) -> HttpResponse:
    entry = get_object_or_404(ShopMemory, pk=memory_id, shop=request.shop)  # type: ignore[attr-defined]
    entry.delete()
    audit("memory.deleted", shop=request.shop, user=request.user, key=entry.key)  # type: ignore[attr-defined]
    messages.success(request, f"Deleted “{entry.key}”.")
    return redirect("dashboard:memory", shop_id=shop_id)
