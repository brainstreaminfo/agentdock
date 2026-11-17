"""factory_boy factories for test data."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import factory
from django.utils import timezone as dj_timezone

from apps.accounts.models import Membership, User
from apps.shops.models import Shop


class UserFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = User
        django_get_or_create = ("email",)

    email = factory.Sequence(lambda n: f"user{n}@example.com")


class ShopFactory(factory.django.DjangoModelFactory):
    """An installed shop with a valid (encrypted) expiring token."""

    class Meta:
        model = Shop
        django_get_or_create = ("domain",)
        skip_postgeneration_save = True

    domain = factory.Sequence(lambda n: f"shop-{n}.myshopify.com")
    name = factory.Sequence(lambda n: f"Shop {n}")
    email = factory.LazyAttribute(lambda o: f"owner@{o.domain}")
    timezone = "UTC"
    granted_scopes = factory.LazyFunction(lambda: ["read_products"])
    installed_at = factory.LazyFunction(dj_timezone.now)

    @factory.post_generation
    def tokens(obj: Shop, create: bool, extracted: Any, **kwargs: Any) -> None:
        if extracted is False:
            return
        obj.set_tokens(
            "shpat_existing",
            expires_in=3600,
            refresh_token="shprt_existing",
            refresh_token_expires_in=int(timedelta(days=90).total_seconds()),
        )
        if create:
            obj.save()


class MembershipFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Membership

    user = factory.SubFactory(UserFactory)
    shop = factory.SubFactory(ShopFactory)
    role = Membership.Role.OWNER
