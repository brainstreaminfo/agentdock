"""Tiny fixed-window rate limiter backed by the Django cache (Redis in production)."""

from django.core.cache import cache


def is_rate_limited(key: str, *, limit: int, window_seconds: int) -> bool:
    """Count one hit for `key`; return True when the limit for this window is exceeded."""
    cache_key = f"rl:{key}"
    added = cache.add(cache_key, 1, timeout=window_seconds)
    if added:
        return False
    try:
        count = cache.incr(cache_key)
    except ValueError:  # key expired between add() and incr()
        cache.set(cache_key, 1, timeout=window_seconds)
        return False
    return count > limit
