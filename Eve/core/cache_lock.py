import secrets
import time
from contextlib import contextmanager

from django.core.cache import cache


class CacheLeaseUnavailable(RuntimeError):
    """Raised when the shared cache cannot coordinate a lease safely."""


@contextmanager
def cache_lease(key: str, *, timeout: int):
    """Yield whether this process owns a short, distributed cache lease."""
    token = secrets.token_urlsafe(24)
    try:
        acquired = cache.add(key, token, timeout=timeout)
    except Exception as exc:
        raise CacheLeaseUnavailable("shared cache unavailable") from exc
    try:
        yield acquired
    finally:
        if acquired:
            try:
                if cache.get(key) == token:
                    cache.delete(key)
            except Exception:
                pass


def wait_for_value(probe, *, timeout: float = 1.0, interval: float = 0.05):
    """Poll briefly for a value produced by the current lease owner."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value is not None:
            return value
        time.sleep(interval)
    return None
