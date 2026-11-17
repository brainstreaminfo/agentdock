"""Magic-link login on our own domain (spec §5.5)."""

from __future__ import annotations

from django import forms
from django.conf import settings
from django.contrib.auth import login, logout
from django.core.mail import send_mail
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme, urlencode
from django.views.decorators.http import require_http_methods, require_POST

from apps.core.audit import audit
from apps.core.logging import get_logger
from apps.core.ratelimit import is_rate_limited

from .models import MagicLinkToken, User

log = get_logger(__name__)


class EmailForm(forms.Form):
    email = forms.EmailField(widget=forms.EmailInput(attrs={"autofocus": True}))


def _safe_next(request: HttpRequest, value: str | None) -> str | None:
    if value and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}):
        return value
    return None


@require_http_methods(["GET", "POST"])
def login_view(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("shops:list")

    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"].lower()
        if is_rate_limited(f"magic:{email}", limit=5, window_seconds=15 * 60):
            form.add_error("email", "Too many login emails. Please wait a few minutes.")
        else:
            user = User.objects.filter(email=email, is_active=True).first()
            # Same response whether or not the account exists (no account enumeration).
            if user is not None:
                raw = MagicLinkToken.objects.issue(user)
                link = settings.APP_URL + reverse("accounts:magic", args=[raw])
                nxt = _safe_next(request, request.GET.get("next"))
                if nxt:
                    link += "?" + urlencode({"next": nxt})
                body = render_to_string("accounts/email_magic_link.txt", {"link": link})
                send_mail(f"Your {settings.APP_NAME} login link", body, None, [email])
                log.info("auth.magic_link_sent", user_id=user.pk)
            else:
                log.info("auth.magic_link_unknown_email")
            return render(request, "accounts/check_email.html", {"email": email})

    return render(request, "accounts/login.html", {"form": form})


def magic_link_view(request: HttpRequest, token: str) -> HttpResponse:
    user = MagicLinkToken.objects.consume(token)
    if user is None:
        return render(request, "accounts/link_invalid.html", status=400)
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    audit("auth.login_magic_link", user=user)
    return redirect(_safe_next(request, request.GET.get("next")) or "shops:list")


@require_POST
def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("accounts:login")
