"""
Agent Studio views (spec §7): dashboard, builder sections, playground, tests,
publish, versions, analytics. All staff-only.

The builder always edits the agent's DRAFT version. Nothing a merchant uses
changes until "Publish" creates a new numbered version.
"""

from __future__ import annotations

import json
from itertools import groupby
from typing import Any

from django.contrib import messages
from django.core.files.storage import default_storage
from django.db import transaction
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from apps.agents.models import AgentDefinition, AgentTestCase, AgentVersion
from apps.agents.prompting import build_system_prompt
from apps.agents.runtime import create_run
from apps.agents.settings_schema import FIELD_TYPES, build_form, schema_to_fields
from apps.agents.tasks import execute_run_task
from apps.agents.testing import CHECK_LABELS, latest_results, start_test_suite, summary
from apps.agents.upgrades import upgrade_installations
from apps.chat.models import Conversation
from apps.chat.rendering import build_items
from apps.core.audit import audit
from apps.core.decorators import staff_required
from apps.runs.models import Run
from apps.shopify_app.scopes import describe
from apps.shops.models import Shop
from apps.tools.models import ToolDefinition

from . import agent_forms as f
from .insights import agent_stats, revenue, runs_per_day, version_diff

SECTIONS = [
    ("listing", "Listing", "id-card"),
    ("brain", "Brain", "brain"),
    ("tools", "Tools", "wrench"),
    ("settings", "Merchant settings", "sliders-horizontal"),
    ("triggers", "Triggers", "zap"),
    ("behaviour", "Behaviour", "shield-check"),
    ("availability", "Availability", "store"),
]
PAGES = [
    ("playground", "Playground", "flask-conical"),
    ("tests", "Tests", "list-checks"),
    ("publish", "Publish", "rocket"),
    ("versions", "Versions", "history"),
    ("analytics", "Analytics", "chart-line"),
]


def _agent(slug: str) -> AgentDefinition:
    return get_object_or_404(AgentDefinition, slug=slug)


def _ctx(agent: AgentDefinition, active: str, **extra: Any) -> dict[str, Any]:
    draft = agent.draft
    latest = agent.latest_version
    return {
        "agent": agent,
        "draft": draft,
        "latest": latest,
        "sections": SECTIONS,
        "pages": PAGES,
        "active": active,
        "unpublished_changes": latest is None or version_diff(latest, draft)["changed"],
        **extra,
    }


# =========================================================================== dashboard
@staff_required
def dashboard(request: HttpRequest) -> HttpResponse:
    stats = agent_stats()
    agents = []
    for agent in AgentDefinition.objects.filter(is_template=False).order_by("name"):
        s = stats.get(agent.slug, {})
        agents.append(
            {
                "agent": agent,
                "latest": agent.latest_version,
                **s,
                "revenue": revenue(agent, s.get("active_shops", 0)),
            }
        )
    templates = AgentDefinition.objects.filter(is_template=True).order_by("name")
    return render(
        request,
        "studio/agents/dashboard.html",
        {"agents": agents, "templates": templates, "form": f.NewAgentForm()},
    )


@staff_required
@require_POST
def agent_create(request: HttpRequest) -> HttpResponse:
    form = f.NewAgentForm(request.POST)
    if not form.is_valid():
        for error in form.errors.values():
            messages.error(request, error.as_text())
        return redirect("studio:home")
    agent = AgentDefinition.objects.create(
        name=form.cleaned_data["name"],
        slug=form.cleaned_data["slug"],
        created_by_id=request.user.pk,
    )
    audit("studio.agent_created", user=request.user, agent=agent.slug)
    return redirect("studio:agent_section", slug=agent.slug, section="brain")


@staff_required
@require_POST
@transaction.atomic
def agent_clone(request: HttpRequest, slug: str) -> HttpResponse:
    source = _agent(slug)
    base = slugify(f"{source.slug}-copy")
    new_slug, n = base, 2
    while AgentDefinition.objects.filter(slug=new_slug).exists():
        new_slug, n = f"{base}-{n}", n + 1
    copy = AgentDefinition.objects.create(
        slug=new_slug,
        name=f"{source.name} (copy)",
        icon=source.icon,
        category=source.category,
        tagline=source.tagline,
        description_md=source.description_md,
        capabilities=source.capabilities,
        example_prompts=source.example_prompts,
        created_by_id=request.user.pk,
    )
    src_draft, dst_draft = source.draft, copy.draft
    for field in AgentVersion.CONFIG_FIELDS:
        setattr(dst_draft, field, getattr(src_draft, field))
    dst_draft.save()
    dst_draft.tools.set(src_draft.tools.all())
    for case in source.test_cases.all():
        AgentTestCase.objects.create(
            agent=copy,
            name=case.name,
            prompt=case.prompt,
            settings=case.settings,
            checks=case.checks,
        )
    audit("studio.agent_cloned", user=request.user, source=source.slug, agent=copy.slug)
    messages.success(request, f"Created “{copy.name}”. Rename it under Listing.")
    return redirect("studio:agent_section", slug=copy.slug, section="listing")


# =========================================================================== builder sections
@staff_required
def agent_section(request: HttpRequest, slug: str, section: str) -> HttpResponse:
    agent = _agent(slug)
    if agent.is_template and request.method == "POST":
        messages.error(request, "Templates are read-only. Clone it to make your own agent.")
        return redirect(request.path)
    handler = SECTION_HANDLERS.get(section)
    if handler is None:
        raise Http404()
    return handler(request, agent)


def _saved(request: HttpRequest, agent: AgentDefinition, section: str, what: str) -> HttpResponse:
    draft = agent.draft
    draft.required_scopes = draft.compute_required_scopes()
    draft.save(update_fields=["required_scopes", "updated_at"])
    audit("studio.draft_saved", user=request.user, agent=agent.slug, section=section)
    messages.success(request, f"{what} saved to the draft.")
    return redirect("studio:agent_section", slug=agent.slug, section=section)


def _listing(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    form = f.ListingForm(request.POST or None, request.FILES or None, instance=agent)
    if request.method == "POST":
        remove = set(request.POST.getlist("remove_screenshot"))
        if form.is_valid():
            agent = form.save(commit=False)
            agent.screenshots = [s for s in agent.screenshots if s not in remove]
            image = form.cleaned_data.get("screenshot")
            if image:
                path = default_storage.save(f"screenshots/{agent.slug}/{image.name}", image)
                agent.screenshots = [*agent.screenshots, default_storage.url(path)]
            agent.save()
            return _saved(request, agent, "listing", "Listing")
    return render(request, "studio/agents/listing.html", _ctx(agent, "listing", form=form))


def _brain(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    draft = agent.draft
    form = f.BrainForm(request.POST or None, instance=draft)
    if request.method == "POST" and form.is_valid():
        form.save()
        return _saved(request, agent, "brain", "Brain")
    preview = None
    shop = Shop.objects.installed().filter(memberships__user_id=request.user.pk or 0).first()
    if shop is not None:
        preview = build_system_prompt(
            draft, shop=shop, agent_settings=draft.settings_defaults or {}
        )
    variables = [
        "{{shop.name}}",
        "{{shop.domain}}",
        "{{shop.currency}}",
        "{{shop.timezone}}",
        "{{today}}",
        "{{memory}}",
        "{{memory.brand_voice}}",
    ]
    variables += [f"{{{{settings.{k}}}}}" for k in (draft.settings_schema.get("properties") or {})]
    return render(
        request,
        "studio/agents/brain.html",
        _ctx(agent, "brain", form=form, preview=preview, preview_shop=shop, variables=variables),
    )


def _tools(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    draft = agent.draft
    if request.method == "POST":
        form = f.ToolsForm(request.POST)
        if form.is_valid():
            draft.tools.set(form.cleaned_data["tools"])
            return _saved(request, agent, "tools", "Tools")
    selected = set(draft.tools.values_list("pk", flat=True))
    tools = ToolDefinition.objects.filter(enabled=True).order_by("category", "key")
    groups = [(cat, list(items)) for cat, items in groupby(tools, key=lambda t: t.category)]
    scopes = [{"scope": s, "label": describe(s)} for s in draft.compute_required_scopes()]
    return render(
        request,
        "studio/agents/tools.html",
        _ctx(agent, "tools", groups=groups, selected=selected, scopes=scopes),
    )


def _settings(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    draft = agent.draft
    errors: list[str] = []
    if request.method == "POST":
        form = f.SettingsBuilderForm(request.POST)
        if form.is_valid():
            draft.settings_schema, draft.settings_defaults = form.schema_and_defaults()
            draft.save(update_fields=["settings_schema", "settings_defaults", "updated_at"])
            return _saved(request, agent, "settings", "Merchant settings")
        errors = [str(e) for e in form.errors.get("fields_json", [])]
        fields = json.loads(request.POST.get("fields_json") or "[]")
    else:
        fields = schema_to_fields(draft.settings_schema or {})
    return render(
        request,
        "studio/agents/settings.html",
        _ctx(
            agent,
            "settings",
            fields_json=json.dumps(fields),
            errors=errors,
            field_types=FIELD_TYPES,
            preview=build_form(draft.settings_schema or {}),
        ),
    )


def _triggers(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    draft = agent.draft
    form = (
        f.TriggersForm(request.POST)
        if request.method == "POST"
        else f.TriggersForm.from_triggers(draft.triggers or {})
    )
    if request.method == "POST" and form.is_valid():
        draft.triggers = form.triggers()
        draft.save(update_fields=["triggers", "updated_at"])
        return _saved(request, agent, "triggers", "Triggers")
    return render(request, "studio/agents/triggers.html", _ctx(agent, "triggers", form=form))


def _behaviour(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    draft = agent.draft
    form = f.BehaviourForm(
        request.POST or None,
        initial={
            "default_autonomy": draft.default_autonomy,
            "allowed_autonomy": draft.allowed_autonomy,
            "output_types": draft.output_types,
        },
    )
    if request.method == "POST" and form.is_valid():
        draft.default_autonomy = form.cleaned_data["default_autonomy"]
        draft.allowed_autonomy = form.cleaned_data["allowed_autonomy"]
        draft.output_types = form.cleaned_data["output_types"]
        draft.save()
        return _saved(request, agent, "behaviour", "Behaviour")
    return render(request, "studio/agents/behaviour.html", _ctx(agent, "behaviour", form=form))


def _availability(request: HttpRequest, agent: AgentDefinition) -> HttpResponse:
    form = f.AvailabilityForm(request.POST or None, instance=agent, initial={"plans": agent.plans})
    if request.method == "POST" and form.is_valid():
        agent = form.save(commit=False)
        agent.plans = form.cleaned_data["plans"]
        agent.save()
        form.save_m2m()
        return _saved(request, agent, "availability", "Availability")
    return render(
        request, "studio/agents/availability.html", _ctx(agent, "availability", form=form)
    )


SECTION_HANDLERS = {
    "listing": _listing,
    "brain": _brain,
    "tools": _tools,
    "settings": _settings,
    "triggers": _triggers,
    "behaviour": _behaviour,
    "availability": _availability,
}


# =========================================================================== playground
@staff_required
def playground(request: HttpRequest, slug: str) -> HttpResponse:
    """Chat with the draft (or a published version) on one of your own stores."""
    agent = _agent(slug)
    shop_form = f.ShopChoiceForm(user=request.user)
    conversation = None
    turns: list[dict[str, Any]] = []
    conv_id = request.GET.get("c")
    if conv_id:
        conversation = get_object_or_404(
            Conversation, pk=conv_id, user_id=request.user.pk, version__agent=agent
        )
        for run in conversation.runs.order_by("id"):
            turns.append(
                {
                    "run": run,
                    "items": build_items(run),
                    "live": run.status in (Run.Status.QUEUED, Run.Status.RUNNING),
                }
            )
    versions = agent.versions.order_by("-is_draft", "-number")
    settings_form = build_form(agent.draft.settings_schema or {})
    return render(
        request,
        "studio/agents/playground.html",
        _ctx(
            agent,
            "playground",
            shop_form=shop_form,
            conversation=conversation,
            turns=turns,
            versions=versions,
            settings_form=settings_form,
            recent=Conversation.objects.filter(user_id=request.user.pk or 0, version__agent=agent)[
                :10
            ],
        ),
    )


@staff_required
@require_POST
def playground_start(request: HttpRequest, slug: str) -> HttpResponse:
    agent = _agent(slug)
    shop_form = f.ShopChoiceForm(request.POST, user=request.user)
    version = get_object_or_404(AgentVersion, agent=agent, pk=request.POST.get("version"))
    settings_form = build_form(version.settings_schema or {}, data=request.POST)
    if not shop_form.is_valid() or not settings_form.is_valid():
        messages.error(request, "Pick one of your stores and fill in the settings.")
        return redirect("studio:playground", slug=slug)
    conversation = Conversation.objects.create(
        shop=shop_form.cleaned_data["shop"],
        user_id=request.user.pk,
        version=version,
        playground_settings=settings_form.cleaned_data,
        title=f"Playground: {agent.name} {version.label}",
    )
    return redirect(reverse("studio:playground", args=[slug]) + f"?c={conversation.pk}")


@staff_required
@require_POST
def playground_send(request: HttpRequest, slug: str, conversation_id: int) -> HttpResponse:
    conversation = get_object_or_404(
        Conversation, pk=conversation_id, user=request.user, version__agent__slug=slug
    )
    if conversation.runs.filter(status__in=[Run.Status.QUEUED, Run.Status.RUNNING]).exists():
        return HttpResponse(
            '<p class="text-sm text-amber-600">Wait for the current answer.</p>', status=409
        )
    text = (request.POST.get("message") or "").strip()[:8000]
    if not text:
        return HttpResponse('<p class="text-sm text-red-600">Type a message first.</p>', status=400)
    run = create_run(
        shop=conversation.shop,
        version=conversation.version,  # type: ignore[arg-type]
        conversation=conversation,
        trigger=Run.Trigger.PLAYGROUND,
        autonomy="APPROVE",
        input={"message": text, "settings": conversation.playground_settings},
    )
    execute_run_task.delay(run.pk)
    run.refresh_from_db()
    return render(
        request, "chat/_turn.html", {"turn": {"run": run, "items": build_items(run), "live": True}}
    )


@staff_required
@require_POST
def playground_trigger(request: HttpRequest, slug: str) -> HttpResponse:
    """Run the draft once as if a schedule fired it (no chat), then show its trace."""
    agent = _agent(slug)
    shop_form = f.ShopChoiceForm(request.POST, user=request.user)
    if not shop_form.is_valid():
        messages.error(request, "Pick one of your stores.")
        return redirect("studio:playground", slug=slug)
    draft = agent.draft
    run = create_run(
        shop=shop_form.cleaned_data["shop"],
        version=draft,
        trigger=Run.Trigger.PLAYGROUND,
        autonomy="APPROVE",
        input={
            "settings": draft.settings_defaults or {},
            "task": "You were started by your schedule. Do your scheduled job now.",
        },
    )
    execute_run_task.delay(run.pk)
    return redirect("studio:run", run_id=run.pk)


# =========================================================================== tests
@staff_required
def tests(request: HttpRequest, slug: str) -> HttpResponse:
    agent = _agent(slug)
    draft = agent.draft
    results = latest_results(agent, draft.config_fingerprint())
    cases = [{"case": c, "result": results.get(c.pk)} for c in agent.test_cases.all()]
    context = _ctx(
        agent,
        "tests",
        cases=cases,
        summary=summary(agent),
        shop_form=f.ShopChoiceForm(user=request.user),
        check_labels=CHECK_LABELS,
    )
    template = (
        "studio/agents/_tests_table.html"
        if request.headers.get("HX-Request")
        else "studio/agents/tests.html"
    )
    return render(request, template, context)


@staff_required
def test_case_edit(request: HttpRequest, slug: str, case_id: int | None = None) -> HttpResponse:
    agent = _agent(slug)
    case = (
        get_object_or_404(AgentTestCase, pk=case_id, agent=agent)
        if case_id
        else AgentTestCase(agent=agent)
    )
    tool_keys = sorted(agent.draft.tools.values_list("key", flat=True))
    form = f.TestCaseForm(request.POST or None, instance=case, tool_keys=tool_keys)
    if request.method == "POST":
        if "delete" in request.POST and case.pk:
            case.delete()
            messages.success(request, "Test case deleted.")
            return redirect("studio:tests", slug=slug)
        if form.is_valid():
            form.save()
            messages.success(request, "Test case saved.")
            return redirect("studio:tests", slug=slug)
    return render(
        request, "studio/agents/test_case.html", _ctx(agent, "tests", form=form, case=case)
    )


@staff_required
@require_POST
def tests_run(request: HttpRequest, slug: str) -> HttpResponse:
    agent = _agent(slug)
    form = f.ShopChoiceForm(request.POST, user=request.user)
    if not form.is_valid():
        messages.error(request, "Pick one of your stores to run the tests on.")
    elif not agent.test_cases.exists():
        messages.error(request, "Add a test case first.")
    else:
        results = start_test_suite(agent, form.cleaned_data["shop"])
        audit("studio.tests_started", user=request.user, agent=slug, count=len(results))
        messages.success(request, f"Running {len(results)} test(s)…")
    return redirect("studio:tests", slug=slug)


# =========================================================================== publish & versions
@staff_required
def publish(request: HttpRequest, slug: str) -> HttpResponse:
    agent = _agent(slug)
    if agent.is_template:
        messages.error(request, "Templates can't be published. Clone it first.")
        return redirect("studio:agent_section", slug=slug, section="listing")
    draft = agent.draft
    form = f.PublishForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        version = draft.publish(user=request.user, changelog=form.cleaned_data["changelog"])
        report = upgrade_installations(version)
        audit(
            "studio.agent_published",
            user=request.user,
            agent=slug,
            version=version.number,
            upgraded=len(report.upgraded),
            needs_scopes=len(report.needs_scopes),
        )
        messages.success(
            request,
            f"Published {version.label}. {len(report.upgraded)} shop(s) upgraded automatically"
            + (
                f", {len(report.needs_scopes)} need to approve new permissions"
                if report.needs_scopes
                else ""
            )
            + (f", {len(report.manual)} will update manually" if report.manual else "")
            + ".",
        )
        return redirect("studio:versions", slug=slug)
    diff = version_diff(agent.latest_version, draft)
    installs = agent.installs.count()
    return render(
        request,
        "studio/agents/publish.html",
        _ctx(
            agent,
            "publish",
            form=form,
            diff=diff,
            tests=summary(agent),
            installs=installs,
            warnings=_publish_warnings(agent, draft),
        ),
    )


def _publish_warnings(agent: AgentDefinition, draft: AgentVersion) -> list[str]:
    warnings = []
    if not draft.instructions_md.strip():
        warnings.append("The instructions are empty.")
    if not draft.tools.exists():
        warnings.append("No tools are selected: the agent can only talk.")
    triggers = draft.triggers or {}
    if not (triggers.get("chat") or triggers.get("schedule_cron") or triggers.get("events")):
        warnings.append("No trigger is enabled: merchants can't use this agent.")
    if not agent.tagline:
        warnings.append("Add a tagline: merchants see it in the Marketplace.")
    return warnings


@staff_required
def versions(request: HttpRequest, slug: str) -> HttpResponse:
    agent = _agent(slug)
    published = agent.versions.filter(is_draft=False).order_by("-number")
    rows = [{"version": v, "installs": v.installs.count()} for v in published]
    return render(request, "studio/agents/versions.html", _ctx(agent, "versions", rows=rows))


@staff_required
@require_POST
def version_restore(request: HttpRequest, slug: str, number: int) -> HttpResponse:
    agent = _agent(slug)
    version = get_object_or_404(AgentVersion, agent=agent, number=number, is_draft=False)
    version.restore_to_draft()
    audit("studio.version_restored", user=request.user, agent=slug, version=number)
    messages.success(request, f"The draft now matches v{number}. Publish it to roll back.")
    return redirect("studio:publish", slug=slug)


# =========================================================================== analytics & runs
@staff_required
def analytics(request: HttpRequest, slug: str) -> HttpResponse:
    agent = _agent(slug)
    stats = agent_stats().get(agent.slug, {})
    series = runs_per_day(agent)
    chart = {
        "type": "bar",
        "data": {
            "labels": series["labels"],
            "datasets": [
                {"label": "Succeeded", "data": series["ok"], "stack": "runs"},
                {"label": "Failed", "data": series["failed"], "stack": "runs"},
            ],
        },
        "options": {
            "responsive": True,
            "maintainAspectRatio": False,
            "scales": {"x": {"stacked": True}, "y": {"stacked": True, "beginAtZero": True}},
        },
    }
    recent = Run.objects.filter(agent_slug=agent.slug).select_related("shop").order_by("-id")[:20]
    return render(
        request,
        "studio/agents/analytics.html",
        _ctx(
            agent,
            "analytics",
            stats=stats,
            chart=json.dumps(chart),
            recent=recent,
            revenue=revenue(agent, stats.get("active_shops", 0)),
        ),
    )


@staff_required
def run_detail(request: HttpRequest, run_id: int) -> HttpResponse:
    """Studio trace for any run (any shop): staff debugging."""
    run = get_object_or_404(Run.objects.select_related("shop", "version__agent"), pk=run_id)
    duration = (
        f"{(run.finished_at - run.started_at).total_seconds():.1f} s"
        if run.started_at and run.finished_at
        else "—"
    )
    return render(
        request,
        "studio/run.html",
        {
            "run": run,
            "steps": run.steps.order_by("idx"),
            "show_traceback": True,
            "tokens": f"{run.tokens_in:,} / {run.tokens_out:,}",
            "duration": duration,
            "live": run.status in (Run.Status.QUEUED, Run.Status.RUNNING),
        },
    )
