"""
Run an agent from the terminal and watch every step (spec §13):

    python manage.py run_agent --shop demo.myshopify.com --agent sales-qa-analyst \\
        --task "How did sales go last week?"

Options:
    --version N|draft   which version (default: the shop's installed version,
                        else the latest published, else the draft)
    --autonomy          SUGGEST | APPROVE | AUTO (default: APPROVE, so changes
                        become approvals you can apply in Django Admin)
    --settings '{"tone": "playful"}'   merchant settings (JSON)

Needs ANTHROPIC_API_KEY (or `ant auth login`). Calls the real Claude API and the
real Shopify store, and costs money.
"""

from __future__ import annotations

import html
import json
import re
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from apps.agents import events
from apps.agents.models import AgentDefinition, AgentInstallation, AgentVersion, Autonomy
from apps.agents.runtime import create_run, execute_run
from apps.runs.models import Run
from apps.shopify_app.hmac import normalize_shop_domain
from apps.shops.models import Shop


class Command(BaseCommand):
    help = "Run an agent once and print each step."

    def create_parser(self, prog_name: str, subcommand: str, **kwargs: Any) -> Any:
        # Django reserves --version (prints Django's version); the spec's CLI uses it
        # for the agent version, so let our option replace Django's.
        return super().create_parser(prog_name, subcommand, conflict_handler="resolve", **kwargs)

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--shop", required=True)
        parser.add_argument("--agent", required=True, help="Agent slug")
        parser.add_argument("--task", required=True)
        parser.add_argument("--version", default="")
        parser.add_argument("--autonomy", default="APPROVE", choices=list(Autonomy.values))
        parser.add_argument("--settings", default="{}")

    def handle(self, *args: Any, **opts: Any) -> None:
        domain = normalize_shop_domain(opts["shop"])
        shop = Shop.objects.filter(domain=domain).first() if domain else None
        if shop is None:
            raise CommandError(f"No shop {opts['shop']}")
        agent = AgentDefinition.objects.filter(slug=opts["agent"]).first()
        if agent is None:
            raise CommandError(f"No agent with slug {opts['agent']}")
        try:
            agent_settings = json.loads(opts["settings"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"--settings is not valid JSON: {exc}") from exc

        installation = AgentInstallation.objects.filter(shop=shop, agent=agent).first()
        version = self._pick_version(agent, opts["version"], installation)
        if installation and installation.version_id != version.pk:
            installation = None  # explicitly asked for another version: run it standalone

        run = create_run(
            shop=shop,
            version=version,
            installation=installation,
            trigger=Run.Trigger.MANUAL,
            autonomy=opts["autonomy"],
            input={"task": opts["task"], "settings": agent_settings},
        )
        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"Run #{run.pk}: {agent.slug} {version.label} on {shop.domain} "
                f"({version.model}, effort {version.effort}, autonomy {run.autonomy})"
            )
        )
        events.add_listener(self._print_event)
        run = execute_run(run)
        self.stdout.write("\n")
        self._print_summary(run)

    def _pick_version(
        self, agent: AgentDefinition, wanted: str, installation: AgentInstallation | None
    ) -> AgentVersion:
        if wanted == "draft":
            return agent.draft
        if wanted:
            version = agent.versions.filter(number=int(wanted), is_draft=False).first()
            if version is None:
                raise CommandError(f"{agent.slug} has no version {wanted}")
            return version
        if installation:
            return installation.version
        return agent.latest_version or agent.draft

    def _print_event(self, run_id: int, name: str, data: dict[str, Any]) -> None:
        if name == "delta":
            text = re.sub(r"<br\s*/?>", "\n", data.get("html", ""))
            self.stdout.write(html.unescape(text), ending="")
        elif name == "tool" and data.get("status") != "running":
            style = self.style.SUCCESS if data["status"] == "ok" else self.style.WARNING
            self.stdout.write(style(f"\n  [tool] {data['key']} -> {data['status']}"))
        elif name == "tool":
            self.stdout.write(f"\n  [tool] {data['key']} …", ending="")

    def _print_summary(self, run: Run) -> None:
        self.stdout.write(self.style.MIGRATE_HEADING("Steps"))
        for step in run.steps.order_by("idx"):
            line = f"  {step.idx:>2}. {step.type:<4} {step.name:<32} {step.duration_ms:>6} ms"
            if step.type == "LLM":
                line += f"  stop={step.output.get('stop_reason')}  ${step.output.get('cost_usd')}"
            else:
                line += f"  {step.output.get('status')}"
            self.stdout.write(line)
            if step.error:
                self.stdout.write(self.style.ERROR("      " + step.error.splitlines()[0]))
        status_style = (
            self.style.SUCCESS if run.status == Run.Status.SUCCEEDED else self.style.ERROR
        )
        self.stdout.write(
            status_style(
                f"{run.status}: {run.tokens_in} tokens in, {run.tokens_out} out, "
                f"${run.cost_usd}" + (f" — {run.error}" if run.error else "")
            )
        )
        pending = run.proposals.filter(status="PENDING").count()
        if pending:
            self.stdout.write(
                f"{pending} change(s) wait for approval: Django Admin → Action proposals."
            )
