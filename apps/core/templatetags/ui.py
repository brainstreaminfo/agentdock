"""
UI template tags. Load with `{% load ui %}`.

    {% icon "package" %}                       -> inline Lucide SVG (16px)
    {% icon "package" class="size-5 text-zinc-400" %}
    {% nav_active "dashboard:home" %}          -> "nav-link-active" when on that page
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

from django import template
from django.conf import settings
from django.template.context import RequestContext
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe

register = template.Library()

_ICON_NAME_RE = re.compile(r"^[a-z0-9-]+$")
_ICON_DIR = Path(settings.BASE_DIR) / "static" / "vendor" / "lucide"


@cache
def _load_icon(name: str) -> str | None:
    if not _ICON_NAME_RE.match(name):
        return None
    path = _ICON_DIR / f"{name}.svg"
    if not path.exists():
        return None
    svg = path.read_text()
    svg = re.sub(r"<!--.*?-->", "", svg, flags=re.S).strip()
    # Drop fixed size/class so we can set our own.
    svg = re.sub(r'\s(width|height|class)="[^"]*"', "", svg, count=3)
    return svg


@register.simple_tag
def icon(name: str, **attrs: str) -> SafeString:
    svg = _load_icon(name) or _load_icon("circle-help") or ""
    css = attrs.get("class", "size-4 shrink-0")
    extra = f' class="{escape(css)}" aria-hidden="true"'
    return mark_safe(svg.replace("<svg", f"<svg{extra}", 1))  # noqa: S308 (trusted local files)


@register.simple_tag(takes_context=True)
def nav_active(context: RequestContext, *url_names: str) -> str:
    request = context.get("request")
    match = getattr(request, "resolver_match", None)
    if match is None:
        return ""
    return "nav-link-active" if match.view_name in url_names else ""
