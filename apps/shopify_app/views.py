"""
Shopify entry points (spec §5).

    GET  /shopify/launch/    App opened from Shopify Admin (HMAC-signed link)
    GET  /shopify/auth/      Start OAuth (install, or request extra scopes)
    GET  /shopify/callback/  OAuth redirect back from Shopify
    POST /shopify/webhooks/  All webhooks (HMAC-signed body)
"""

from __future__ import annotations

import json
import secrets
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth import login
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from apps.accounts.models import Membership
from apps.core.audit import audit
from apps.core.logging import bind_context, get_logger
from apps.shops.models import Shop

from . import hmac, oauth, services, tasks
from . import scopes as scope_utils
from .models import WebhookEvent

log = get_logger(__name__)

SESSION_STATE = "shopify_oauth_state"
SESSION_SHOP = "shopify_oauth_shop"
SESSION_NEXT = "shopify_post_auth_redirect"


def _error(request: HttpRequest, title: str, message: str, status: int = 400) -> HttpResponse:
    return render(
        request, "shopify_app/error.html", {"title": title, "message": message}, status=status
    )


def _auth_url(shop_domain: str, **extra: str) -> str:
    return reverse("shopify:auth") + "?" + urlencode({"shop": shop_domain, **extra})


# --------------------------------------------------------------------------- launch
@require_GET
def launch(request: HttpRequest) -> HttpResponse:
    """
    Merchant clicked our app in Shopify Admin. Shopify adds `shop`, `timestamp`,
    `host` and an `hmac` signature to the URL.
    """
    params = hmac.querydict_to_lists(request.GET)
    shop_domain = hmac.normalize_shop_domain(request.GET.get("shop"))
    if shop_domain is None:
        return _error(request, "Invalid store", "The store address is missing or invalid.")
    bind_context(shop=shop_domain)

    if not hmac.verify_query_hmac(params):
        log.warning("launch.bad_hmac")
        return _error(request, "Invalid link", "This link was not signed by Shopify.", 403)

    shop = Shop.objects.filter(domain=shop_domain).first()
    # Not installed yet / uninstalled / missing base scopes  ->  OAuth.
    if shop is None or not shop.is_installed or not shop.has_scopes(settings.SHOPIFY_BASE_SCOPES):
        return redirect(_auth_url(shop_domain))

    # Already logged in as someone with access to this shop -> straight in.
    user = request.user
    if user.is_authenticated and Membership.objects.filter(user=user, shop=shop).exists():
        return redirect("dashboard:home", shop_id=shop.pk)

    # Otherwise log in the shop owner. Guard against replaying a leaked launch URL:
    # it must be recent AND each signature may only be used once.
    if not hmac.is_fresh_timestamp(params, settings.SHOPIFY_LAUNCH_MAX_AGE_SECONDS):
        log.info("launch.stale_timestamp")
        return redirect(_auth_url(shop_domain))
    signature = params["hmac"][0]
    if not cache.add(
        f"launch-hmac:{signature}", 1, timeout=settings.SHOPIFY_LAUNCH_MAX_AGE_SECONDS * 2
    ):
        log.info("launch.replayed_signature")
        return redirect(_auth_url(shop_domain))

    owner = (
        Membership.objects.filter(shop=shop, role=Membership.Role.OWNER, user__is_active=True)
        .select_related("user")
        .order_by("created_at")
        .first()
    )
    if owner is None:
        return redirect(_auth_url(shop_domain))
    login(request, owner.user, backend="django.contrib.auth.backends.ModelBackend")
    audit("auth.login_shopify_launch", shop=shop, user=owner.user)
    return redirect("dashboard:home", shop_id=shop.pk)


# --------------------------------------------------------------------------- oauth
@require_GET
def auth(request: HttpRequest) -> HttpResponse:
    """
    Start OAuth. `?shop=x.myshopify.com[&scopes=write_products,read_orders][&next=/path]`

    We always request the UNION of base scopes + scopes already granted + the
    extra ones, because Shopify replaces the grant with exactly what we ask for.
    """
    shop_domain = hmac.normalize_shop_domain(request.GET.get("shop"))
    if shop_domain is None:
        return _error(request, "Invalid store", "Enter your store as yourstore.myshopify.com.")

    extra = scope_utils.parse(request.GET.get("scopes"))
    invalid = [s for s in extra if not scope_utils.is_valid(s)]
    if invalid:
        return _error(request, "Invalid permissions", f"Unknown scope(s): {', '.join(invalid)}")

    existing = Shop.objects.filter(domain=shop_domain).first()
    granted = existing.granted_scopes if existing and existing.is_installed else []
    scopes = scope_utils.union(settings.SHOPIFY_BASE_SCOPES, granted, extra)

    state = secrets.token_urlsafe(32)
    request.session[SESSION_STATE] = state
    request.session[SESSION_SHOP] = shop_domain
    nxt = request.GET.get("next")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        request.session[SESSION_NEXT] = nxt

    log.info("oauth.start", shop=shop_domain, scopes=scopes)
    return redirect(oauth.build_authorize_url(shop_domain, scopes, state))


@require_GET
def callback(request: HttpRequest) -> HttpResponse:
    params = hmac.querydict_to_lists(request.GET)
    shop_domain = hmac.normalize_shop_domain(request.GET.get("shop"))
    if shop_domain is None:
        return _error(request, "Invalid store", "The store address is missing or invalid.")
    bind_context(shop=shop_domain)

    if not hmac.verify_query_hmac(params):
        log.warning("oauth.callback_bad_hmac")
        return _error(request, "Invalid request", "This request was not signed by Shopify.", 403)

    expected_state = request.session.pop(SESSION_STATE, None)
    expected_shop = request.session.pop(SESSION_SHOP, None)
    state = request.GET.get("state", "")
    if (
        not expected_state
        or not secrets.compare_digest(expected_state, state)
        or expected_shop != shop_domain
    ):
        # Usually the session cookie expired or was blocked. We show a button rather
        # than redirecting automatically, so broken cookies can't cause a redirect loop.
        log.info("oauth.callback_state_mismatch")
        return render(
            request,
            "shopify_app/error.html",
            {
                "title": "Let's try that again",
                "message": "Your session expired while connecting your store.",
                "retry_url": _auth_url(shop_domain),
            },
            status=400,
        )

    code = request.GET.get("code")
    if not code:
        return _error(request, "Install cancelled", "Shopify did not return an authorization code.")

    try:
        tokens = oauth.exchange_code(shop_domain, code)
        result = services.complete_install(
            shop_domain=shop_domain,
            tokens=tokens,
            logged_in_user=request.user if request.user.is_authenticated else None,
        )
    except Exception:
        log.exception("oauth.install_failed")
        return _error(
            request,
            "We couldn't finish connecting your store",
            "Please try again from your Shopify Admin. If it keeps happening, contact support.",
            status=502,
        )

    login(request, result.user, backend="django.contrib.auth.backends.ModelBackend")
    tasks.sync_shop.delay(result.shop.pk)
    log.info("oauth.installed", shop_id=result.shop.pk, new=result.is_new_install)

    # e.g. resume a pending agent install after a scope upgrade (Phase 5).
    nxt = request.session.pop(SESSION_NEXT, None)
    if nxt:
        return redirect(nxt)
    if result.is_new_install:
        return redirect("dashboard:onboarding", shop_id=result.shop.pk)
    return redirect("dashboard:home", shop_id=result.shop.pk)


# --------------------------------------------------------------------------- webhooks
@csrf_exempt  # Shopify can't send a CSRF token; the HMAC proves authenticity instead.
@require_POST
def webhook(request: HttpRequest) -> HttpResponse:
    body = request.body
    if not hmac.verify_webhook_hmac(body, request.headers.get("X-Shopify-Hmac-Sha256")):
        log.warning("webhook.bad_hmac", topic=request.headers.get("X-Shopify-Topic"))
        return HttpResponse(status=401)  # Shopify requires 401 for invalid HMAC

    topic = (request.headers.get("X-Shopify-Topic") or "").strip()
    shop_domain = hmac.normalize_shop_domain(request.headers.get("X-Shopify-Shop-Domain"))
    event_id = request.headers.get("X-Shopify-Event-Id") or request.headers.get(
        "X-Shopify-Webhook-Id"
    )
    if not topic or shop_domain is None or not event_id:
        return HttpResponseBadRequest("Missing Shopify webhook headers")

    try:
        payload = json.loads(body or b"{}")
    except json.JSONDecodeError:
        return HttpResponseBadRequest("Invalid JSON")

    event, created = WebhookEvent.objects.get_or_create(
        shopify_event_id=event_id,
        defaults={
            "shop": Shop.objects.filter(domain=shop_domain).first(),
            "shop_domain": shop_domain,
            "topic": topic,
            "api_version": request.headers.get("X-Shopify-API-Version", ""),
            "payload": payload,
        },
    )
    if not created:
        log.info("webhook.duplicate", topic=topic, event_id=event_id)
        return HttpResponse(status=200)

    log.info("webhook.received", topic=topic, shop=shop_domain, webhook_event_id=event.pk)
    # Respond fast (Shopify times out after 5 s); do the work in Celery.
    tasks.process_webhook_event.delay(event.pk)
    return HttpResponse(status=200)
