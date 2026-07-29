import logging
import time
from functools import wraps

from django.conf import settings
from django.core.cache import cache
from django.http import HttpResponse

logger = logging.getLogger(__name__)


def _effective_limit(limit: int) -> int:
    """Apply RATE_LIMIT_SCALE (1 everywhere except load-test environments;
    a deploy check refuses anything else in production)."""
    return max(1, int(limit * getattr(settings, "RATE_LIMIT_SCALE", 1)))


def _current_count(key_prefix: str, ident: str, limit: int, window_seconds: int):
    """Increment and return the fixed-window counter, or None if the cache
    backend is unreachable (fail open: availability over throttling, loudly
    logged so monitoring catches a dead Redis)."""
    window = int(time.time() // window_seconds)
    key = f"ratelimit:{key_prefix}:{ident}:{window}"
    try:
        if cache.add(key, 1, timeout=window_seconds):
            return 1
        try:
            return cache.incr(key)
        except ValueError:  # key expired between add and incr
            cache.add(key, 1, timeout=window_seconds)
            return 1
    except Exception:
        logger.exception("Rate-limit cache unavailable; failing open")
        return None


def rate_limit(key_prefix: str, limit: int, window_seconds: int):
    """Fixed-window per-IP rate limit for POST requests.

    Keyed on REMOTE_ADDR, not X-Forwarded-For, which clients can spoof.
    Backed by Django's cache — Redis in production (see REDIS_URL), so the
    window is shared across all workers.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method == "POST":
                ident = request.META.get("REMOTE_ADDR", "unknown")
                effective = _effective_limit(limit)
                count = _current_count(key_prefix, ident, effective, window_seconds)
                if count is not None and count > effective:
                    return HttpResponse(
                        "Too many attempts. Please try again later.",
                        status=429,
                    )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
