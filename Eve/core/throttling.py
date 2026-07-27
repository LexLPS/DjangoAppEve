import time
from functools import wraps

from django.core.cache import cache
from django.http import HttpResponse


def rate_limit(key_prefix: str, limit: int, window_seconds: int):
    """Fixed-window per-IP rate limit for POST requests.

    Keyed on REMOTE_ADDR, not X-Forwarded-For, which clients can spoof.
    Backed by Django's cache: with the default LocMemCache the window is
    per-process, so configure a shared cache (Redis/Memcached) in production
    to make the limit global across workers.
    """
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(request, *args, **kwargs):
            if request.method == "POST":
                ident = request.META.get("REMOTE_ADDR", "unknown")
                window = int(time.time() // window_seconds)
                key = f"ratelimit:{key_prefix}:{ident}:{window}"
                if cache.add(key, 1, timeout=window_seconds):
                    count = 1
                else:
                    try:
                        count = cache.incr(key)
                    except ValueError:  # key expired between add and incr
                        cache.add(key, 1, timeout=window_seconds)
                        count = 1
                if count > limit:
                    return HttpResponse(
                        "Too many attempts. Please try again later.",
                        status=429,
                    )
            return view_func(request, *args, **kwargs)
        return wrapper
    return decorator
