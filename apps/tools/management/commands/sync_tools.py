"""Copy built-in tools into the database:  python manage.py sync_tools  (runs on deploy)."""

from typing import Any

from django.core.management.base import BaseCommand

from apps.tools.sync import sync_tools


class Command(BaseCommand):
    help = "Sync built-in tools (code registry) into ToolDefinition rows."

    def handle(self, *args: Any, **opts: Any) -> None:
        report = sync_tools()
        self.stdout.write(
            self.style.SUCCESS(
                f"Tools synced: {len(report.created)} created, {len(report.updated)} updated, "
                f"{len(report.removed)} disabled (no longer in code)."
            )
        )
        for key in report.removed:
            self.stdout.write(f"  disabled: {key}")
