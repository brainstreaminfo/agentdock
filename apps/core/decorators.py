"""
Access-control decorators used on every view (spec §12).

    @require_shop_access()               -> any member of the shop
    @require_shop_access(role="ADMIN")   -> ADMIN or OWNER
    @staff_required                      -> Agent Studio (is_staff users)

`require_shop_access` reads `shop_id` from the URL, loads the Shop, checks the
logged-in user has a Membership with a sufficient role, and passes the Shop to the
view as `request.shop`. Any other shop id returns 404 (not 403) so we don't reveal
which shop ids exist.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from typing import Any

from django.contrib.auth.views import redirect_to_login
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse

from apps.core.logging import bind_context

View = Callable[..., HttpResponse]


def require_shop_access(role: str | None = None) -> Callable[[View], View]:
    from apps.accounts.models import Membership

    def decorator(view: View) -> View:
        @wraps(view)
        def wrapper(request: HttpRequest, shop_id: int, *args: Any, **kwargs: Any) -> HttpResponse:
            if not request.user.is_authenticated:
                return redirect_to_login(request.get_full_path())
            membership = (
                Membership.objects.select_related("shop")
                .filter(user=request.user, shop_id=shop_id, shop__uninstalled_at__isnull=True)
                .first()
            )
            if membership is None:
                raise Http404("Shop not found")
            if role is not None and not membership.has_role(role):
                raise PermissionDenied("Your role does not allow this action.")
            request.shop = membership.shop  # type: ignore[attr-defined]
            request.membership = membership  # type: ignore[attr-defined]
            request.session["last_shop_id"] = membership.shop_id
            bind_context(shop_id=membership.shop_id)
            return view(request, shop_id, *args, **kwargs)

        return wrapper

    return decorator


def staff_required(view: View) -> View:
    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if not request.user.is_authenticated:
            return redirect_to_login(request.get_full_path())
        if not request.user.is_staff:
            raise Http404()
        return view(request, *args, **kwargs)

    return wrapper
