"""
Errors a tool may raise. The executor turns them into a clear message for the
AI (never a crash), and records them in the run trace.
"""

from __future__ import annotations

from typing import Any


class ToolError(Exception):
    """Something the AI should know about and can maybe fix (bad id, not found…)."""

    def __init__(self, message: str, *, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class MissingScopesError(ToolError):
    """The shop hasn't granted the Shopify permissions this tool needs."""

    def __init__(self, scopes: list[str]) -> None:
        self.scopes = sorted(scopes)
        super().__init__(
            "This store hasn't given the app permission for: "
            + ", ".join(self.scopes)
            + ". The merchant has been asked to grant access. Tell them this step "
            "is blocked until they do; don't retry."
        )


class ShopifyUserError(ToolError):
    """Shopify refused a mutation (validation `userErrors`)."""
