"""
Structured logging (structlog).

How to use in any module:

    from apps.core.logging import get_logger
    log = get_logger(__name__)
    log.info("shop.synced", products=12)

Every line automatically carries whatever context is "bound" for the current
request or Celery task: `request_id`, `user_id`, `shop_id`, `task_id`, and later
`agent` / `run_id`. Bind more with `bind_context(run_id=...)`.

Secrets and personal data are scrubbed by `redact_processor` before anything is
written: keys like `token`/`secret`/`password` are replaced, and email
addresses / phone numbers inside strings are masked.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

# ---------------------------------------------------------------- redaction
_SENSITIVE_KEY_PARTS = ("token", "secret", "password", "authorization", "api_key", "cookie")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Phone numbers: international "+44 20 7946 0958" or North-American "(555) 123-4567".
# Deliberately does NOT match bare digit runs, so Shopify IDs
# (gid://shopify/Product/1234567890) stay readable in logs.
_PHONE_RE = re.compile(r"(?<![\w/])\+\d[\d\s().-]{7,}\d|\(?\b\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b")


def redact_text(value: str) -> str:
    """Mask emails and phone numbers inside a string."""
    value = _EMAIL_RE.sub("[email]", value)
    return _PHONE_RE.sub("[phone]", value)


def redact_value(key: str, value: Any) -> Any:
    """Recursively redact a value. Sensitive keys are replaced entirely."""
    if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [redact_value(key, v) for v in value]
    return value


def redact_processor(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict.keys()):
        if key in ("timestamp", "level", "logger"):
            continue
        event_dict[key] = redact_value(key, event_dict[key])
    return event_dict


# ---------------------------------------------------------------- context helpers
def get_logger(name: str | None = None) -> Any:
    return structlog.get_logger(name)


def bind_context(**values: Any) -> None:
    """Attach values to every log line for the rest of this request / task."""
    structlog.contextvars.bind_contextvars(**values)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------- configuration
_SHARED_PROCESSORS: list[Any] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    redact_processor,
]


def build_logging_config(level: str = "INFO", json: bool = True) -> dict[str, Any]:
    """
    Configure structlog and return a Django `LOGGING` dict that routes the
    standard library loggers (Django, Celery, httpx) through the same renderer.
    """
    renderer: Any = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=True)
    )

    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "structlog": {
                "()": structlog.stdlib.ProcessorFormatter,
                "processors": [
                    structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                    structlog.processors.format_exc_info,
                    renderer,
                ],
                "foreign_pre_chain": _SHARED_PROCESSORS,
            }
        },
        "handlers": {
            "console": {"class": "logging.StreamHandler", "formatter": "structlog"},
        },
        "root": {"handlers": ["console"], "level": level},
        "loggers": {
            "django.db.backends": {"level": "WARNING"},
            "httpx": {"level": "WARNING"},
            "httpcore": {"level": "WARNING"},
            "celery": {"level": "INFO"},
        },
    }


# ---------------------------------------------------------------- celery hooks
def bind_celery_task_context(task_id: str | None, task_name: str | None) -> None:
    clear_context()
    bind_context(task_id=task_id, task_name=task_name)


__all__ = [
    "bind_celery_task_context",
    "bind_context",
    "build_logging_config",
    "clear_context",
    "get_logger",
    "redact_text",
    "redact_value",
]

logging.getLogger(__name__)  # make sure stdlib logging is imported/initialised
