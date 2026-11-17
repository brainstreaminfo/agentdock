"""Write AuditLog rows: `audit("shop.installed", shop=shop, user=user, scopes=[...])`."""

from __future__ import annotations

from typing import Any

from apps.core.logging import get_logger, redact_value
from apps.core.models import AuditLog

log = get_logger(__name__)


def audit(action: str, *, shop: Any = None, user: Any = None, **meta: Any) -> AuditLog:
    safe_meta = {k: redact_value(k, v) for k, v in meta.items()}
    log.info("audit", action=action, shop_id=getattr(shop, "pk", None), **safe_meta)
    if user is not None and not getattr(user, "is_authenticated", False):
        user = None
    return AuditLog.objects.create(action=action, shop=shop, user=user, meta=safe_meta)
