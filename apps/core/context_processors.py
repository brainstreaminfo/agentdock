from typing import Any

from django.conf import settings
from django.http import HttpRequest


def app(request: HttpRequest) -> dict[str, Any]:
    return {"APP_NAME": settings.APP_NAME, "APP_URL": settings.APP_URL}
