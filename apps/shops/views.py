"""Store list / switcher and "Connect another store" (spec §5.5)."""

from __future__ import annotations

from urllib.parse import urlencode

from django import forms
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse

from apps.shopify_app.hmac import normalize_shop_domain

from .models import Shop


class ConnectStoreForm(forms.Form):
    shop = forms.CharField(
        label="Store address",
        help_text="Your store's .myshopify.com address",
        widget=forms.TextInput(attrs={"placeholder": "your-store.myshopify.com"}),
    )

    def clean_shop(self) -> str:
        raw = self.cleaned_data["shop"].strip().lower()
        raw = raw.removeprefix("https://").removeprefix("http://").split("/")[0]
        if "." not in raw:
            raw = f"{raw}.myshopify.com"
        domain = normalize_shop_domain(raw)
        if domain is None:
            raise forms.ValidationError("Enter a valid address like your-store.myshopify.com")
        return domain


@login_required
def store_list(request: HttpRequest) -> HttpResponse:
    user_shops = Shop.objects.installed().filter(memberships__user_id=request.user.pk or 0)
    shops = list(user_shops.order_by("name"))
    # One store -> go straight to it (unless the user explicitly asked for the list).
    if len(shops) == 1 and "all" not in request.GET:
        return redirect("dashboard:home", shop_id=shops[0].pk)
    return render(request, "shops/store_list.html", {"shops": shops, "form": ConnectStoreForm()})


@login_required
def connect_store(request: HttpRequest) -> HttpResponse:
    form = ConnectStoreForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        query = urlencode({"shop": form.cleaned_data["shop"]})
        return redirect(f"{reverse('shopify:auth')}?{query}")
    return render(request, "shops/connect.html", {"form": form})
