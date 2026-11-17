"""
Live run events: the worker writes them, the browser reads them (SSE).

    emit(run_id, "delta", {"html": "Hel"})      # worker side
    for event in read(run_id, last_id): ...     # web side (async: aread)

We use a Redis *Stream* (not pub/sub): events are stored for an hour, so a
browser that connects late, or reconnects after a network blip, gets every
event it missed (SSE `Last-Event-ID`). With pub/sub those would be lost.

Event names used by the runtime:
    status   {"text": "Thinking…"}                 short status line
    delta    {"html": "..."}                       streamed answer text (escaped)
    tool     {"html": "..."}                       a tool call started/finished
    done     {"html": "...", "status": "..."}      final rendered message; stream ends
    failed   {"html": "...", "message": "..."}     run failed/cancelled; stream ends
                                                   (not "error": browsers use that name
                                                   for EventSource connection errors)

Tests use the in-memory backend (settings.RUN_EVENTS_BACKEND = "memory").
"""

from __future__ import annotations

import json
import threading
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from typing import Any

from django.conf import settings

TTL_SECONDS = 60 * 60
MAX_EVENTS = 5000
FINAL_EVENTS = ("done", "failed")


@dataclass
class RunEvent:
    id: str
    name: str
    data: dict[str, Any]

    def to_sse(self) -> str:
        payload = json.dumps(self.data, ensure_ascii=False)
        return f"id: {self.id}\nevent: {self.name}\ndata: {payload}\n\n"


def _key(run_id: int) -> str:
    return f"agenthub:run:{run_id}:events"


# --------------------------------------------------------------------------- redis
class RedisEvents:
    def __init__(self, url: str) -> None:
        import redis

        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.url = url

    def emit(self, run_id: int, name: str, data: dict[str, Any]) -> str:
        key = _key(run_id)
        event_id = self.client.xadd(
            key, {"name": name, "data": json.dumps(data)}, maxlen=MAX_EVENTS, approximate=True
        )
        self.client.expire(key, TTL_SECONDS)
        return str(event_id)

    def read(self, run_id: int, last_id: str = "0", block_ms: int = 0) -> list[RunEvent]:
        result = self.client.xread({_key(run_id): last_id}, block=block_ms or None, count=200)
        return _parse(result)

    async def aread(self, run_id: int, last_id: str = "0", block_ms: int = 15000) -> list[RunEvent]:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(self.url, decode_responses=True)
        try:
            result = await client.xread({_key(run_id): last_id}, block=block_ms, count=200)
        finally:
            await client.aclose()
        return _parse(result)


def _parse(result: Any) -> list[RunEvent]:
    events = []
    for _stream, entries in result or []:
        for event_id, fields in entries:
            events.append(RunEvent(str(event_id), fields["name"], json.loads(fields["data"])))
    return events


# --------------------------------------------------------------------------- memory (tests)
class MemoryEvents:
    def __init__(self) -> None:
        self.streams: dict[int, list[RunEvent]] = defaultdict(list)
        self.lock = threading.Lock()

    def emit(self, run_id: int, name: str, data: dict[str, Any]) -> str:
        with self.lock:
            stream = self.streams[run_id]
            event = RunEvent(f"{len(stream) + 1}-0", name, data)
            stream.append(event)
            return event.id

    def read(self, run_id: int, last_id: str = "0", block_ms: int = 0) -> list[RunEvent]:
        last = int(last_id.split("-")[0]) if last_id else 0
        return list(self.streams[run_id][last:])

    async def aread(self, run_id: int, last_id: str = "0", block_ms: int = 0) -> list[RunEvent]:
        return self.read(run_id, last_id)

    def names(self, run_id: int) -> list[str]:
        return [e.name for e in self.streams[run_id]]


@cache
def _memory() -> MemoryEvents:
    return MemoryEvents()


def backend() -> RedisEvents | MemoryEvents:
    if settings.RUN_EVENTS_BACKEND == "memory":
        return _memory()
    return _redis(settings.REDIS_URL)


@cache
def _redis(url: str) -> RedisEvents:
    return RedisEvents(url)


_listeners: list[Any] = []


def add_listener(fn: Any) -> None:
    """Also call fn(run_id, name, data) for every event (used by `manage.py run_agent`)."""
    _listeners.append(fn)


def emit(run_id: int, name: str, data: dict[str, Any]) -> str:
    for listener in _listeners:
        listener(run_id, name, data)
    return backend().emit(run_id, name, data)
