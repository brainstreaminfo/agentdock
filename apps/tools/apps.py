from django.apps import AppConfig


class ToolsConfig(AppConfig):
    name = "apps.tools"
    label = "tools"

    def ready(self) -> None:
        # Importing the catalog runs every @tool decorator, filling the registry.
        from apps.tools import catalog  # noqa: F401
