"""Request middleware: binds request_id / user_id / shop_id into every log line."""

from __future__ import annotations

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from apps.core.logging import bind_context, clear_context


class RequestLogContextMiddleware:
    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        clear_context()
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        bind_context(request_id=request_id, path=request.path)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            bind_context(user_id=user.pk)
        response = self.get_response(request)
        response["X-Request-ID"] = request_id
        return response
