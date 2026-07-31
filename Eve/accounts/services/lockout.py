"""Per-username account lockout, shared by every credential endpoint.

Complements the per-IP rate limit: a credential-stuffing run distributed
across many IPs never trips a per-IP throttle, but still locks the account
it targets. This lives in a service rather than in a view because more than
one entry point accepts a password — the HTML login form and the API token
endpoint — and a lockout that only guards one of them is not a lockout.

Fails open when the cache is unavailable (availability over brute-force
protection), logging loudly so monitoring can alert; see
docs/OBSERVABILITY.md.
"""
import logging

from django.contrib.auth.models import User
from django.core.cache import cache

logger = logging.getLogger(__name__)

LOCKOUT_THRESHOLD = 10
LOCKOUT_WINDOW_SECONDS = 900


def lockout_key(username: str) -> str:
    return f"lockout:{username.strip().lower()}"


def is_locked(username: str) -> bool:
    try:
        return (cache.get(lockout_key(username)) or 0) >= LOCKOUT_THRESHOLD
    except Exception:
        logger.exception("Lockout cache unavailable; failing open")
        return False


def record_failure(username: str) -> int:
    """Count a failed attempt; returns the new count (0 if uncountable)."""
    key = lockout_key(username)
    try:
        if cache.add(key, 1, timeout=LOCKOUT_WINDOW_SECONDS):
            return 1
        return cache.incr(key)
    except Exception:
        logger.exception("Lockout cache unavailable; failure not recorded")
        return 0


def clear_failures(username: str):
    try:
        cache.delete(lockout_key(username))
    except Exception:
        logger.exception("Lockout cache unavailable; could not clear failures")


def notify_lockout(username: str):
    """Tell the real account owner their account was just locked (threat
    model R5). Silent when the username does not exist — no enumeration
    signal."""
    user = User.objects.filter(username=username).first()
    if not user or not user.email:
        return
    from accounts.tasks import send_lockout_email

    try:
        send_lockout_email.delay(user.pk)
    except Exception:
        logger.exception("Could not queue lockout notification")


def register_failure(username: str):
    """Record a failed credential attempt and notify once on crossing the
    threshold. Every password-accepting endpoint calls this."""
    failures = record_failure(username)
    if failures == LOCKOUT_THRESHOLD:  # exactly once per window
        logger.warning(
            "Account lockout triggered", extra={"event": "account_lockout"}
        )
        notify_lockout(username)
    return failures
