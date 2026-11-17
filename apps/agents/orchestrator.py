"""
"My Team": the chat orchestrator (spec §8.4).

The merchant can chat with all their agents at once. The orchestrator is an
ordinary agent run with one difference: its tools are built AT RUN TIME from the
shop's installed, chat-enabled agents, one `delegate_to_<agent-slug>` tool each,
described with the agent's tagline and description. Installing or removing
agents changes the team without any code change.

When the orchestrator calls a delegate tool, `delegate()` runs that agent as a
CHILD run (same worker, `parent_run` set) with the task the orchestrator wrote,
and returns the agent's answer plus the ids of any changes it proposed. Those
changes show up as approval cards in the My Team chat.

The orchestrator itself is a hidden system agent (`AgentDefinition.is_system`),
created on first use by `team_version()`. Edit its model or instructions in
Django Admin if needed; it never appears in the Studio or the Marketplace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from django.db import transaction

from apps.core.logging import get_logger
from apps.tools.untrusted import frame_for_llm

from .models import AgentDefinition, AgentInstallation, AgentVersion, Effort

if TYPE_CHECKING:
    from apps.runs.models import Run
    from apps.shops.models import Shop

log = get_logger(__name__)

TEAM_SLUG = "my-team"
TEAM_NAME = "My Team"
DELEGATE_PREFIX = "delegate_to_"
# How a delegation is named in the trace and in chat ("delegate:sales-analyst").
DELEGATE_STEP_PREFIX = "delegate:"
MAX_TASK_CHARS = 8000

TEAM_INSTRUCTIONS = """\
You coordinate the merchant's AI agents. You can't look at or change the store
yourself: you work by delegating to the agents listed as tools (`delegate_to_…`).

- Pick the agent whose description fits each part of the request. Use several
  agents when the request has several parts; call them one after another when
  one needs another's result.
- Each agent only sees the task you write. Make it complete and self-contained:
  include the relevant details from the conversation (names, numbers, dates,
  what the merchant wants).
- Combine the agents' answers into one short, clear reply. Say which agent did
  what when it helps.
- When an agent proposed store changes, tell the merchant they're shown below
  for approval. Never claim a change is done unless the agent says so.
- If no agent fits, say so and suggest installing one from the Marketplace.
"""


# --------------------------------------------------------------------------- the team agent
@transaction.atomic
def team_version() -> AgentVersion:
    """The orchestrator's published version (created the first time)."""
    agent, _ = AgentDefinition.objects.get_or_create(
        slug=TEAM_SLUG,
        defaults={
            "name": TEAM_NAME,
            "icon": "users",
            "tagline": "Chat with all your agents at once.",
            "is_system": True,
            "visibility": AgentDefinition.Visibility.PRIVATE,
        },
    )
    version = agent.latest_version
    if version is None:
        draft = agent.draft
        draft.instructions_md = TEAM_INSTRUCTIONS
        draft.effort = Effort.MEDIUM
        draft.max_steps = 10
        draft.triggers = {"chat": True, "schedule_cron": "", "events": []}
        draft.save()
        version = draft.publish(changelog="Built-in orchestrator")
    return version


def is_team(version: AgentVersion | None) -> bool:
    return version is not None and version.agent.is_system and version.agent.slug == TEAM_SLUG


# --------------------------------------------------------------------------- team members
def team_members(shop: Shop) -> list[AgentInstallation]:
    """Installed, enabled, chat-enabled agents of the shop (the team)."""
    installs = (
        AgentInstallation.objects.for_shop(shop)
        .filter(enabled=True, agent__is_system=False)
        .select_related("agent", "version")
        .order_by("agent__slug")
    )
    return [i for i in installs if i.version.chat_enabled]


def delegate_tool_name(slug: str) -> str:
    # Tool names may contain letters, digits, _ and - (max 128).
    return (DELEGATE_PREFIX + slug)[:128]


def delegate_tools(shop: Shop) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """(tool definitions for the API, {tool name: installation id})."""
    definitions = []
    names: dict[str, int] = {}
    for inst in team_members(shop):
        agent = inst.agent
        description = f'Delegate a task to the agent "{agent.name}".'
        if agent.tagline:
            description += f" {agent.tagline}"
        if agent.description_md:
            description += "\n\nAbout this agent:\n" + agent.description_md[:1500]
        if agent.capabilities:
            description += "\n\nIt can:\n" + "\n".join(f"- {c}" for c in agent.capabilities[:10])
        name = delegate_tool_name(agent.slug)
        definitions.append(
            {
                "name": name,
                "description": description,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "Complete, self-contained instructions for the agent.",
                        }
                    },
                    "required": ["task"],
                },
            }
        )
        names[name] = inst.pk
    return definitions, names


# --------------------------------------------------------------------------- delegation
def delegate(parent: Run, installation_id: int, raw_input: dict[str, Any]) -> dict[str, Any]:
    """
    Run one team member as a child run and return a tool result dict:
        {"status": "ok"|"error", "data": {"agent", "reply", "proposals": [uuid…]}, "error"}
    """
    from apps.runs.models import ActionProposal, Run

    from .runtime import create_run, execute_run

    inst = (
        AgentInstallation.objects.select_related("agent", "version", "shop")
        .filter(pk=installation_id, shop_id=parent.shop_id, enabled=True)
        .first()
    )
    if inst is None:
        return {"status": "error", "error": "This agent is no longer installed or is paused."}
    task = str((raw_input or {}).get("task") or "").strip()[:MAX_TASK_CHARS]
    if not task:
        return {"status": "error", "error": "Write the task for the agent in `task`."}

    child = create_run(
        shop=inst.shop,
        version=inst.version,
        installation=inst,
        trigger=parent.trigger,
        input={"task": task},
        parent_run=parent,
    )
    log.info("team.delegate", agent=inst.agent.slug, child_run_id=child.pk)
    child = execute_run(child)
    proposals = [
        str(u) for u in ActionProposal.objects.filter(run=child).values_list("uuid", flat=True)
    ]
    data = {
        "agent": inst.agent.name,
        "slug": inst.agent.slug,
        "run_id": child.pk,
        "reply": (child.output or {}).get("text", ""),
        "proposals": proposals,
    }
    if child.status != Run.Status.SUCCEEDED:
        return {
            "status": "error",
            "data": data,
            "error": f"{inst.agent.name} could not finish: {child.error or child.status}",
        }
    return {"status": "ok", "data": data}


def result_for_llm(result: dict[str, Any]) -> str:
    """The agent's reply is based on store data, so it's framed as untrusted data too."""
    return frame_for_llm(result)
