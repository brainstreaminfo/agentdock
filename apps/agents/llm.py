"""
The LLM behind agents, behind a small interface (spec §3: `LLMProvider`).

    provider = get_provider()
    response = provider.stream_turn(params, on_text=lambda t: ...)

`params` is a Messages API request body built by the runtime. The provider
streams ONE model turn, calls `on_text` for each piece of answer text, and
returns the final message as plain dicts (`LLMResponse`). Tests replace the
provider with a scripted fake, so no test ever calls Anthropic.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cache
from typing import Any, Protocol

from django.conf import settings

from apps.core.logging import get_logger

log = get_logger(__name__)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
TextCallback = Callable[[str], None]


@dataclass
class LLMResponse:
    content: list[dict[str, Any]]  # content blocks as dicts (text, thinking, tool_use, …)
    stop_reason: str | None
    model: str
    usage: dict[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    stop_details: dict[str, Any] | None = None


class LLMError(Exception):
    """The model call failed (network, API error, unparseable stream…)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LLMProvider(Protocol):
    def stream_turn(self, params: dict[str, Any], on_text: TextCallback) -> LLMResponse: ...


# --------------------------------------------------------------------------- Anthropic
class AnthropicProvider:
    """Claude via the official `anthropic` SDK, always streaming."""

    def __init__(self, client: Any = None) -> None:
        import anthropic

        self._anthropic = anthropic
        if client is not None:  # tests pass a client with a mock transport
            self.client = client
            return
        # Credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile.
        kwargs: dict[str, Any] = {"max_retries": 3}
        if settings.ANTHROPIC_API_KEY:
            kwargs["api_key"] = settings.ANTHROPIC_API_KEY
        self.client = anthropic.Anthropic(**kwargs)

    def stream_turn(self, params: dict[str, Any], on_text: TextCallback) -> LLMResponse:
        anthropic = self._anthropic
        params = dict(params)
        if params.get("model") in settings.LLM_FALLBACK_MODELS:
            # If a safety classifier declines, Anthropic re-runs the turn on a
            # fallback model inside the same call (see DECISIONS.md).
            params["betas"] = [*params.get("betas", []), FALLBACK_BETA]
            params["fallbacks"] = "default"

        # With eager input streaming, a tool input the SDK can't parse at all
        # raises ValueError from the stream; re-issue the turn (bounded).
        for attempt in range(3):
            try:
                with self.client.beta.messages.stream(**params) as stream:
                    for event in stream:
                        if event.type == "text":
                            on_text(event.text)
                    message = stream.get_final_message()
                break
            except ValueError as exc:
                log.warning("llm.unparseable_tool_input", attempt=attempt + 1)
                if attempt == 2:
                    raise LLMError("The model produced an unreadable tool call.") from exc
            except anthropic.RateLimitError as exc:
                raise LLMError("The AI service is busy (rate limited).", retryable=True) from exc
            except anthropic.APIStatusError as exc:
                raise LLMError(
                    f"AI service error {exc.status_code}: {exc.message}",
                    retryable=exc.status_code >= 500,
                ) from exc
            except anthropic.APIConnectionError as exc:
                raise LLMError("Can't reach the AI service.", retryable=True) from exc

        stop_details = getattr(message, "stop_details", None)
        return LLMResponse(
            content=[block.to_dict(mode="json", exclude_none=True) for block in message.content],
            stop_reason=message.stop_reason,
            model=message.model,
            usage=message.usage.to_dict(mode="json", exclude_none=True),
            request_id=getattr(message, "_request_id", None),
            stop_details=stop_details.to_dict(mode="json") if stop_details else None,
        )


_override: LLMProvider | None = None


def set_provider(provider: LLMProvider | None) -> None:
    """Tests: replace the provider (None restores the real one)."""
    global _override
    _override = provider


def get_provider() -> LLMProvider:
    return _override or _anthropic_provider()


@cache
def _anthropic_provider() -> AnthropicProvider:
    return AnthropicProvider()


# --------------------------------------------------------------------------- cost
def cost_usd(model: str, usage: dict[str, Any]) -> Decimal:
    """Price one turn from its `usage` block (USD). Unknown models cost 0 and log a warning."""
    prices = settings.MODEL_PRICING.get(model)
    if prices is None:
        log.warning("llm.unknown_model_price", model=model)
        return Decimal(0)

    def tokens(name: str) -> Decimal:
        return Decimal(int(usage.get(name) or 0))

    per_million = Decimal(1_000_000)
    total = (
        tokens("input_tokens") * Decimal(str(prices["input"]))
        + tokens("output_tokens") * Decimal(str(prices["output"]))
        + tokens("cache_creation_input_tokens") * Decimal(str(prices["cache_write"]))
        + tokens("cache_read_input_tokens") * Decimal(str(prices["cache_read"]))
    ) / per_million
    return total.quantize(Decimal("0.000001"))


# --------------------------------------------------------------------------- history
_INTERNAL_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use"}
_KNOWN_TYPES = {
    "text",
    "thinking",
    "redacted_thinking",
    "tool_use",
    "fallback",
    "server_tool_use",
    "web_search_tool_result",
    "web_fetch_tool_result",
}


def content_for_history(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    The assistant content to append to the conversation, unchanged EXCEPT after a
    mid-output refusal fallback: then Anthropic requires us to drop thinking,
    tool_use and other model-internal blocks that appear BEFORE the last
    `fallback` block, while keeping the `fallback` block exactly where it was.
    """
    last_fallback = max(
        (i for i, block in enumerate(content) if block.get("type") == "fallback"), default=-1
    )
    if last_fallback < 0:
        return content
    kept = []
    for i, block in enumerate(content):
        kind = block.get("type")
        if i < last_fallback and (
            kind in _INTERNAL_BEFORE_FALLBACK
            or kind not in _KNOWN_TYPES
            or kind == "server_tool_use"
        ):
            continue
        kept.append(block)
    return kept
