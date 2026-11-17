"""Create/update the 3 example agent templates:  python manage.py seed_agent_templates"""

from typing import Any

from django.core.management.base import BaseCommand

from apps.agents.seed import seed_templates


class Command(BaseCommand):
    help = "Create or update the example agent templates (never published)."

    def handle(self, *args: Any, **opts: Any) -> None:
        for agent in seed_templates():
            draft = agent.draft
            self.stdout.write(
                f"  {agent.slug:<30} {draft.tools.count()} tools, scopes: "
                + (", ".join(draft.required_scopes) or "none")
            )
        self.stdout.write(self.style.SUCCESS("Agent templates are ready."))
