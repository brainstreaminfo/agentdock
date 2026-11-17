"""Makes `current_shop` and `my_shops` (for the store switcher) available in templates."""

from typing import Any

from django.http import HttpRequest


def current_shop(request: HttpRequest) -> dict[str, Any]:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from apps.shops.models import Shop

    return {
        "current_shop": getattr(request, "shop", None),
        "current_membership": getattr(request, "membership", None),
        # Lazy queryset: only hits the DB if a template actually uses it.
        "my_shops": Shop.objects.installed().filter(memberships__user=user).order_by("name"),
    }
