"""Throttles that fail open when the cache is unreachable.

DRF's throttles talk to the Django cache directly and let the exception
escape, turning a Redis outage into a 500 on every API request. The
storefront's own limiter already fails open and logs loudly
(`core.throttling`); the API now behaves the same way, so one policy holds
across both clients: availability first, with the degradation alerted on
rather than silent.
"""
import logging

from rest_framework import throttling

logger = logging.getLogger(__name__)


class FailOpenMixin:
    def allow_request(self, request, view):
        try:
            return super().allow_request(request, view)
        except Exception:
            logger.exception(
                "Rate-limit cache unavailable; allowing request",
                extra={"event": "throttle_fail_open", "scope": getattr(self, "scope", "")},
            )
            return True


class AnonRateThrottle(FailOpenMixin, throttling.AnonRateThrottle):
    pass


class UserRateThrottle(FailOpenMixin, throttling.UserRateThrottle):
    pass


class ScopedRateThrottle(FailOpenMixin, throttling.ScopedRateThrottle):
    pass
