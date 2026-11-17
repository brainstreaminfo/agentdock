"""Makes `current_shop` and `my_shops` (for the store switcher) available in templates."""

from typing import Any

from django.http import HttpRequest


def current_shop(request: HttpRequest) -> dict[str, Any]:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {}
    from apps.shops.models import Shop

    shop = getattr(request, "shop", None)
    return {
        "current_shop": shop,
        # Sidebar badges (pending approvals, unread alerts); lazy, only for shop pages.
        "nav_counts": _NavCounts(shop) if shop is not None else {},
        "current_membership": getattr(request, "membership", None),
        # Lazy queryset: only hits the DB if a template actually uses it.
        "my_shops": Shop.objects.installed().filter(memberships__user=user).order_by("name"),
    }


class _NavCounts:
    """Counts for sidebar badges, computed only when a template reads them."""

    def __init__(self, shop: Any) -> None:
        self.shop = shop

    @property
    def pending(self) -> int:
        from apps.runs.models import ActionProposal

        return ActionProposal.objects.for_shop(self.shop).filter(status="PENDING").count()

    @property
    def alerts(self) -> int:
        from apps.dashboard.models import Alert

        return Alert.objects.for_shop(self.shop).filter(read_at__isnull=True).count()
