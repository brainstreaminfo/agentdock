"""
Merchant pages for agents (spec §8.1-8.3).

    /s/<shop>/marketplace/                   grid: search, categories, featured
    /s/<shop>/marketplace/<slug>/            agent detail + permissions it needs
    /s/<shop>/marketplace/<slug>/install/    install flow (permissions → settings → save)
    /s/<shop>/agents/                        My agents
    /s/<shop>/agents/<id>/                   settings, history, run now, pause,
                                             update, uninstall

Installing, configuring and updating need the ADMIN role (or OWNER); members
can browse and see their agents. Business rules live in `installs.py`.
"""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import messages
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from apps.agents.models import AgentInstallation, AgentVersion
from apps.agents.settings_schema import build_form
from apps.agents.upgrades import pending_update
from apps.core.decorators import require_shop_access
from apps.runs.models import ActionProposal, Run
from apps.tools.context import ToolContext
from apps.tools.executor import execute_tool
from apps.triggers.schedules import next_run_at

from . import catalog, installs
from .installs import InstallError

# --------------------------------------------------------------------------- forms


class BehaviourForm(forms.Form):
    """Autonomy + schedule switch, shown under the agent's own settings form."""

    autonomy = forms.ChoiceField(label="How should this agent act?", widget=forms.RadioSelect)
    schedule_enabled = forms.BooleanField(label="Run on its schedule", required=False)

    def __init__(self, *args: Any, version: AgentVersion, **kwargs: Any) -> None:
        kwargs.setdefault("prefix", "agent")  # never clashes with a setting's key
        super().__init__(*args, **kwargs)
        autonomy = self.fields["autonomy"]
        assert isinstance(autonomy, forms.ChoiceField)
        autonomy.choices = installs.allowed_autonomy(version)
        self.fields["autonomy"].help_text = AUTONOMY_HELP
        if not version.schedule_cron:
            del self.fields["schedule_enabled"]
        else:
            self.fields[
                "schedule_enabled"
            ].help_text = f"Schedule: {version.schedule_cron} (cron, in your store's timezone)."


AUTONOMY_HELP = (
    "Suggest only: it only advises. Ask before changing: every store change waits for "
    "your approval. Act automatically: changes apply right away and can be undone."
)


def _settings_form(request: HttpRequest, version: AgentVersion, initial: dict | None = None):  # type: ignore[no-untyped-def]
    shop_id = request.shop.pk  # type: ignore[attr-defined]
    return build_form(
        version.settings_schema or {},
        data=request.POST if request.method == "POST" else None,
        initial=initial,
        picker_urls={
            "product": reverse("marketplace:picker", args=[shop_id, "product"]),
            "collection": reverse("marketplace:picker", args=[shop_id, "collection"]),
        },
    )


def _can_manage(request: HttpRequest) -> bool:
    return request.membership.has_role("ADMIN")  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- marketplace
@require_shop_access()
def home(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    query = request.GET.get("q", "").strip()[:100]
    category = request.GET.get("category", "").strip()[:60]
    agents = list(catalog.search(shop, query, category))
    installed = set(AgentInstallation.objects.for_shop(shop).values_list("agent_id", flat=True))
    browsing = not query and not category
    return render(
        request,
        "marketplace/home.html",
        {
            "agents": [a for a in agents if not (browsing and a.featured)],
            "featured": [a for a in agents if a.featured] if browsing else [],
            "categories": catalog.categories(shop),
            "query": query,
            "category": category,
            "installed": installed,
        },
    )


@require_shop_access()
def detail(request: HttpRequest, shop_id: int, slug: str) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    agent = catalog.get_visible_agent(shop, slug)
    version = agent.latest_version
    assert version is not None
    return render(
        request,
        "marketplace/detail.html",
        {
            "agent": agent,
            "version": version,
            "permissions": catalog.permission_lines(version.required_scopes, shop.granted_scopes),
            "installation": AgentInstallation.objects.for_shop(shop).filter(agent=agent).first(),
            "tools": version.tools.order_by("category", "name"),
            "can_manage": _can_manage(request),
        },
    )


@require_shop_access(role="ADMIN")
def install(request: HttpRequest, shop_id: int, slug: str) -> HttpResponse:
    """
    Step 1: plan check. Step 2: missing permissions → "needs access to…" page.
    Step 3: settings form → POST creates the installation.
    """
    shop = request.shop  # type: ignore[attr-defined]
    agent = catalog.get_visible_agent(shop, slug)
    check = installs.check_install(shop, agent)
    if check.installation is not None:
        return redirect("marketplace:agent", shop_id=shop.pk, installation_id=check.installation.pk)
    pending = request.session.get(installs.SESSION_PENDING) or {}
    resumed = pending.get("agent") == slug and pending.get("shop_id") == shop.pk

    if not check.entitlement:
        return render(
            request, "marketplace/upsell.html", {"agent": agent, "decision": check.entitlement}
        )
    if check.missing_scopes:
        return render(
            request,
            "marketplace/permissions.html",
            {
                "agent": agent,
                "permissions": catalog.permission_lines(check.missing_scopes),
                # Came back from Shopify but some access still isn't granted.
                "declined": resumed,
                "grant_action": reverse("marketplace:grant", args=[shop.pk, slug]),
            },
        )

    version = check.version
    settings_form = _settings_form(request, version)
    behaviour = BehaviourForm(
        request.POST or None,
        version=version,
        initial={
            "autonomy": version.default_autonomy,
            "schedule_enabled": bool(version.schedule_cron),
        },
    )
    error = ""
    if request.method == "POST" and settings_form.is_valid() and behaviour.is_valid():
        try:
            inst = installs.install_agent(
                shop,
                agent,
                settings=settings_form.cleaned_data,
                autonomy=behaviour.cleaned_data["autonomy"],
                schedule_enabled=behaviour.cleaned_data.get("schedule_enabled", False),
                user=request.user,
            )
        except InstallError as exc:
            error = str(exc)
        else:
            request.session.pop(installs.SESSION_PENDING, None)
            messages.success(request, f"{agent.name} is installed.")
            return redirect("marketplace:agent", shop_id=shop.pk, installation_id=inst.pk)
    return render(
        request,
        "marketplace/install.html",
        {
            "agent": agent,
            "version": version,
            "settings_form": settings_form,
            "behaviour": behaviour,
            "error": error,
            "resumed": resumed,
        },
    )


@require_shop_access(role="ADMIN")
@require_POST
def grant(request: HttpRequest, shop_id: int, slug: str) -> HttpResponse:
    """Remember the install, then send the merchant to Shopify to approve more access."""
    shop = request.shop  # type: ignore[attr-defined]
    agent = catalog.get_visible_agent(shop, slug)
    check = installs.check_install(shop, agent)
    back = reverse("marketplace:install", args=[shop.pk, slug])
    if not check.missing_scopes:
        return redirect(back)
    request.session[installs.SESSION_PENDING] = {
        "shop_id": shop.pk,
        "agent": slug,
        "version": check.version.number,
    }
    return redirect(installs.grant_url(shop, check.missing_scopes, back))


# --------------------------------------------------------------------------- my agents
def _installation(request: HttpRequest, installation_id: int) -> AgentInstallation:
    return get_object_or_404(
        AgentInstallation.objects.select_related("agent", "version", "shop"),
        pk=installation_id,
        shop=request.shop,  # type: ignore[attr-defined]
    )


@require_shop_access()
def my_agents(request: HttpRequest, shop_id: int) -> HttpResponse:
    shop = request.shop  # type: ignore[attr-defined]
    rows = []
    pending = dict(
        ActionProposal.objects.for_shop(shop)
        .filter(status=ActionProposal.Status.PENDING, run__installation__isnull=False)
        .values_list("run__installation")
        .annotate(n=Count("id"))
    )
    for inst in AgentInstallation.objects.for_shop(shop).select_related("agent", "version", "shop"):
        latest, missing = pending_update(inst)
        rows.append(
            {
                "inst": inst,
                "next_run": next_run_at(inst),
                "pending": pending.get(inst.pk, 0),
                "update": latest,
                "update_needs_scopes": bool(missing),
            }
        )
    return render(request, "marketplace/my_agents.html", {"rows": rows})


@require_shop_access()
def agent_page(request: HttpRequest, shop_id: int, installation_id: int) -> HttpResponse:
    inst = _installation(request, installation_id)
    can_manage = _can_manage(request)
    if request.method == "POST" and not can_manage:
        raise Http404()
    settings_form = _settings_form(request, inst.version, initial=inst.effective_settings())
    behaviour = BehaviourForm(
        request.POST or None,
        version=inst.version,
        initial={"autonomy": inst.autonomy, "schedule_enabled": inst.schedule_enabled},
    )
    error = ""
    if request.method == "POST" and settings_form.is_valid() and behaviour.is_valid():
        try:
            installs.update_settings(
                inst,
                settings=settings_form.cleaned_data,
                autonomy=behaviour.cleaned_data["autonomy"],
                schedule_enabled=behaviour.cleaned_data.get("schedule_enabled", False),
                user=request.user,
            )
        except InstallError as exc:
            error = str(exc)
        else:
            messages.success(request, "Settings saved.")
            return redirect("marketplace:agent", shop_id=shop_id, installation_id=inst.pk)
    latest, missing = pending_update(inst)
    runs = (
        Run.objects.for_shop(inst.shop)
        .filter(installation=inst)
        .exclude(trigger__in=[Run.Trigger.PLAYGROUND, Run.Trigger.TEST, Run.Trigger.TOOL_TEST])
        .order_by("-created_at")[:20]
    )
    return render(
        request,
        "marketplace/agent.html",
        {
            "inst": inst,
            "agent": inst.agent,
            "settings_form": settings_form,
            "behaviour": behaviour,
            "error": error,
            "can_manage": can_manage,
            "runs": runs,
            "next_run": next_run_at(inst),
            "update": latest,
            "update_permissions": catalog.permission_lines(missing) if missing else [],
            "pending": ActionProposal.objects.for_shop(inst.shop)
            .filter(run__installation=inst, status=ActionProposal.Status.PENDING)
            .count(),
        },
    )


@require_shop_access(role="ADMIN")
@require_POST
def run_now(request: HttpRequest, shop_id: int, installation_id: int) -> HttpResponse:
    inst = _installation(request, installation_id)
    try:
        run = installs.run_now(inst, user=request.user, task=request.POST.get("task", "")[:4000])
    except InstallError as exc:
        messages.error(request, str(exc))
        return redirect("marketplace:agent", shop_id=shop_id, installation_id=inst.pk)
    return redirect("chat:trace", shop_id=shop_id, run_id=run.pk)


@require_shop_access(role="ADMIN")
@require_POST
def toggle(request: HttpRequest, shop_id: int, installation_id: int) -> HttpResponse:
    inst = _installation(request, installation_id)
    installs.set_enabled(inst, not inst.enabled, user=request.user)
    messages.success(request, f"{inst.agent.name} is {'running' if inst.enabled else 'paused'}.")
    nxt = request.POST.get("next", "")
    if not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        nxt = reverse("marketplace:agents", args=[shop_id])
    return redirect(nxt)


@require_shop_access(role="ADMIN")
@require_POST
def uninstall(request: HttpRequest, shop_id: int, installation_id: int) -> HttpResponse:
    inst = _installation(request, installation_id)
    name = inst.agent.name
    installs.uninstall(inst, user=request.user)
    messages.success(request, f"{name} was uninstalled.")
    return redirect("marketplace:agents", shop_id=shop_id)


@require_shop_access(role="ADMIN")
@require_POST
def update(request: HttpRequest, shop_id: int, installation_id: int) -> HttpResponse:
    """Update to the newest version; if it needs new permissions, go through Shopify first."""
    inst = _installation(request, installation_id)
    latest, missing = pending_update(inst)
    here = reverse("marketplace:agent", args=[shop_id, inst.pk])
    if latest is None:
        messages.info(request, "This agent is already up to date.")
        return redirect(here)
    if missing:
        # Shopify sends the merchant back here; they click Update again (now allowed).
        return redirect(installs.grant_url(inst.shop, missing, here))
    try:
        installs.update_to_latest(inst, user=request.user)
    except InstallError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, f"Updated {inst.agent.name} to {latest.label}.")
    return redirect(here)


# --------------------------------------------------------------------------- picker search
PICKER_TOOLS = {
    "product": ("products.search", "products"),
    "collection": ("collections.list", "collections"),
}


@require_shop_access()
def picker(request: HttpRequest, shop_id: int, kind: str) -> HttpResponse:
    """HTMX: search products/collections for the settings picker (components/picker.html)."""
    if kind not in PICKER_TOOLS:
        raise Http404()
    tool_key, list_key = PICKER_TOOLS[kind]
    query = request.GET.get("q", "").strip()[:100]
    result = execute_tool(
        ToolContext(shop=request.shop, autonomy="SUGGEST"),  # type: ignore[attr-defined]
        tool_key,
        {"query": query, "limit": 10},
    )
    results = []
    if result.ok and isinstance(result.data, dict):
        results = [
            {"id": item.get("id"), "title": item.get("title") or item.get("id")}
            for item in result.data.get(list_key) or []
            if item.get("id")
        ]
    return render(
        request,
        "marketplace/_picker_results.html",
        {"results": results, "noun": kind, "error": "" if result.ok else result.error},
    )
