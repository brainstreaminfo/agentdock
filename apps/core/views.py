from django.db import connection
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect


def home(request: HttpRequest) -> HttpResponse:
    if request.user.is_authenticated:
        return redirect("shops:list")
    return redirect("accounts:login")


def healthz(request: HttpRequest) -> JsonResponse:
    """Liveness/readiness probe for the hosting platform."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse({"ok": True})
