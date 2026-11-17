"""Magic-link login, store switcher, and access control / no cross-shop access (spec §12)."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from django.core import mail
from django.http import HttpResponse
from django.test import RequestFactory
from django.utils import timezone

from apps.accounts.models import MagicLinkToken, Membership
from apps.core.decorators import require_shop_access, staff_required
from tests.factories import MembershipFactory, ShopFactory, UserFactory

pytestmark = pytest.mark.django_db


def magic_link_from_outbox() -> str:
    match = re.search(r"https://app\.test(/login/[^\s]+)", mail.outbox[-1].body)
    assert match
    return match.group(1)


# ------------------------------------------------------------------ magic link
def test_magic_link_login(client, owner) -> None:
    resp = client.post("/login/", {"email": owner.email.upper()})
    assert resp.status_code == 200 and b"Check your email" in resp.content
    link = magic_link_from_outbox()

    resp = client.get(link)

    assert resp.status_code == 302
    assert int(client.session["_auth_user_id"]) == owner.pk


def test_magic_link_single_use(client, owner) -> None:
    client.post("/login/", {"email": owner.email})
    link = magic_link_from_outbox()
    client.get(link)
    client.logout()
    assert client.get(link).status_code == 400


def test_magic_link_expires(client, owner) -> None:
    raw = MagicLinkToken.objects.issue(owner)
    MagicLinkToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert client.get(f"/login/{raw}/").status_code == 400


def test_unknown_email_gets_same_page_but_no_email(client, db) -> None:
    resp = client.post("/login/", {"email": "nobody@example.com"})
    assert b"Check your email" in resp.content
    assert mail.outbox == []


def test_magic_link_rate_limited(client, owner) -> None:
    for _ in range(5):
        client.post("/login/", {"email": owner.email})
    resp = client.post("/login/", {"email": owner.email})
    assert b"Too many" in resp.content
    assert len(mail.outbox) == 5


def test_magic_link_next_is_preserved_but_not_open_redirect(client, owner) -> None:
    client.post("/login/?next=/stores/", {"email": owner.email})
    assert client.get(magic_link_from_outbox())["Location"] == "/stores/"
    client.logout()
    client.post("/login/?next=https://evil.com/", {"email": owner.email})
    assert "evil" not in client.get(magic_link_from_outbox())["Location"]


def test_logout_requires_post(client, owner) -> None:
    client.force_login(owner)
    assert client.get("/logout/").status_code == 405
    client.post("/logout/")
    assert "_auth_user_id" not in client.session


# ------------------------------------------------------------------ stores
def test_single_store_redirects_to_dashboard(client, shop, owner) -> None:
    client.force_login(owner)
    assert client.get("/stores/")["Location"] == f"/s/{shop.pk}/"


def test_store_list_shows_only_my_stores(client, shop, owner) -> None:
    other = ShopFactory(name="Someone Else's Shop")
    mine = MembershipFactory(user=owner, shop=ShopFactory(name="My Second Shop")).shop
    client.force_login(owner)
    content = client.get("/stores/").content.decode()
    assert shop.name in content and mine.name in content
    assert other.name not in content


def test_connect_store_normalises_input(client, owner) -> None:
    client.force_login(owner)
    resp = client.post("/stores/connect/", {"shop": "https://New-Store.myshopify.com/admin"})
    assert resp["Location"] == "/shopify/auth/?shop=new-store.myshopify.com"
    resp = client.post("/stores/connect/", {"shop": "bare-name"})
    assert resp["Location"] == "/shopify/auth/?shop=bare-name.myshopify.com"
    assert client.post("/stores/connect/", {"shop": "evil.com"}).status_code == 200  # form error


# ------------------------------------------------------------------ access control
@pytest.mark.parametrize("path", ["/s/{id}/", "/s/{id}/settings/", "/s/{id}/welcome/"])
def test_no_cross_shop_access(client, shop, owner, path) -> None:
    other_shop = ShopFactory()
    client.force_login(owner)
    assert client.get(path.format(id=shop.pk)).status_code == 200
    assert client.get(path.format(id=other_shop.pk)).status_code == 404


def test_anonymous_redirected_to_login(client, shop) -> None:
    resp = client.get(f"/s/{shop.pk}/")
    assert resp.status_code == 302 and resp["Location"].startswith("/login/?next=")


def test_staff_without_membership_cannot_open_merchant_pages(client, shop) -> None:
    staff = UserFactory(is_staff=True)
    client.force_login(staff)
    assert client.get(f"/s/{shop.pk}/").status_code == 404


def test_role_requirement(rf: RequestFactory, shop) -> None:
    member = MembershipFactory(shop=shop, role=Membership.Role.MEMBER).user

    @require_shop_access(role="ADMIN")
    def admin_only(request, shop_id):
        return HttpResponse("ok")

    request = rf.get("/")
    request.user = member
    request.session = {}  # type: ignore[assignment]
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        admin_only(request, shop.pk)

    Membership.objects.filter(user=member).update(role=Membership.Role.OWNER)
    assert admin_only(request, shop.pk).status_code == 200


def test_staff_required(rf: RequestFactory, db) -> None:
    @staff_required
    def studio(request):
        return HttpResponse("studio")

    request = rf.get("/studio/")
    request.user = UserFactory()
    from django.http import Http404

    with pytest.raises(Http404):
        studio(request)
    request.user = UserFactory(is_staff=True)
    assert studio(request).status_code == 200


def test_settings_page_never_shows_tokens(client, shop, owner) -> None:
    client.force_login(owner)
    content = client.get(f"/s/{shop.pk}/settings/").content.decode()
    assert "shpat_" not in content and shop.access_token_enc not in content
    assert "View your products" in content


def test_healthz(client, db) -> None:
    assert client.get("/healthz/").json() == {"ok": True}
