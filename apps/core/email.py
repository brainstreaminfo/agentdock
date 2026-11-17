"""
Email backend that sends through Resend's HTTP API (https://resend.com).

Configured as EMAIL_BACKEND in base settings; dev falls back to printing emails
in the console when RESEND_API_KEY is empty.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from django.conf import settings
from django.core.mail.backends.base import BaseEmailBackend
from django.core.mail.message import EmailMessage, EmailMultiAlternatives

from apps.core.logging import get_logger

log = get_logger(__name__)
RESEND_URL = "https://api.resend.com/emails"


class ResendEmailBackend(BaseEmailBackend):
    def send_messages(self, email_messages: Sequence[EmailMessage]) -> int:
        sent = 0
        for message in email_messages:
            payload: dict[str, Any] = {
                "from": message.from_email or settings.DEFAULT_FROM_EMAIL,
                "to": list(message.to),
                "subject": message.subject,
                "text": message.body,
            }
            if isinstance(message, EmailMultiAlternatives):
                for content, mimetype in message.alternatives:
                    if mimetype == "text/html":
                        payload["html"] = content
            try:
                resp = httpx.post(
                    RESEND_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.RESEND_API_KEY}"},
                    timeout=10,
                )
                resp.raise_for_status()
                sent += 1
            except httpx.HTTPError as exc:
                log.error("email.send_failed", subject=message.subject, error=str(exc))
                if not self.fail_silently:
                    raise
        return sent
