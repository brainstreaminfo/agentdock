"""Merchant dashboard (spec §8.5). Phase 1 is the shell; widgets arrive in Phase 5."""

from __future__ import annotations

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render

from apps.core.decorators import require_shop_access
from apps.shopify_app import scopes as scope_utils


@require_shop_access()
def home(request: HttpRequest, shop_id: int) -> HttpResponse:
    return render(request, "dashboard/home.html")


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
