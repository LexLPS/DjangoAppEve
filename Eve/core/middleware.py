import logging
import time
import uuid

from django.conf import settings
from django.db import connection
from django.http import Http404

from .logging import request_id_var

request_logger = logging.getLogger("eve.requests")


class RequestIDMiddleware:
    """Assign a correlation ID to every request.

    The ID is generated server-side (inbound X-Request-ID is untrusted and
    ignored), stamped on every log record via RequestIdFilter, and returned
    as X-Request-ID so users/support can quote it.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid.uuid4().hex
        token = request_id_var.set(request_id)
        try:
            response = self.get_response(request)
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            request_id_var.reset(token)


class _QueryStats:
    def __init__(self):
        self.count = 0
        self.seconds = 0.0

    def __call__(self, execute, sql, params, many, context):
        started = time.monotonic()
        try:
            return execute(sql, params, many, context)
        finally:
            self.count += 1
            self.seconds += time.monotonic() - started


class RequestMetricsMiddleware:
    """Emit one structured log event per request: latency, status, and
    database time/queries. Error rates and latency percentiles are derived
    from these events in the log platform (see docs/OBSERVABILITY.md)."""

    SKIP_PREFIXES = ("/healthz", "/static/")
    SLOW_REQUEST_MS = 1000

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path.startswith(self.SKIP_PREFIXES):
            return self.get_response(request)

        stats = _QueryStats()
        started = time.monotonic()
        with connection.execute_wrapper(stats):
            response = self.get_response(request)
        duration_ms = round((time.monotonic() - started) * 1000, 1)

        route = getattr(getattr(request, "resolver_match", None), "route", "") or request.path
        level = logging.WARNING if duration_ms > self.SLOW_REQUEST_MS else logging.INFO
        request_logger.log(
            level,
            "%s %s -> %d in %sms",
            request.method, route, response.status_code, duration_ms,
            extra={
                "event": "http_request",
                "method": request.method,
                "route": route,
                "status": response.status_code,
                "duration_ms": duration_ms,
                "db_queries": stats.count,
                "db_ms": round(stats.seconds * 1000, 1),
            },
        )
        return response


class TrustedProxyMiddleware:
    """Resolve the real client IP behind trusted reverse proxies.

    When the direct peer (REMOTE_ADDR) is one of TRUSTED_PROXIES, walk
    X-Forwarded-For from the right, skip trusted hops, and use the first
    untrusted address as REMOTE_ADDR. Client-supplied XFF prefixes are
    thereby ignored — only the hop appended by our own proxy counts.
    With TRUSTED_PROXIES empty (the default) nothing is rewritten.
    Must run before anything that reads REMOTE_ADDR (rate limiting,
    admin allowlist).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        trusted = set(settings.TRUSTED_PROXIES)
        peer = request.META.get("REMOTE_ADDR", "")
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if trusted and peer in trusted and forwarded:
            hops = [ip.strip() for ip in forwarded.split(",") if ip.strip()]
            for ip in reversed(hops):
                if ip not in trusted:
                    request.META["REMOTE_ADDR"] = ip
                    break
        return self.get_response(request)


class AdminIPAllowlistMiddleware:
    """Optional IP allowlist for the admin interface.

    Active only when ADMIN_ALLOWED_IPS is non-empty; otherwise the admin is
    expected to sit behind VPN/SSO at the reverse proxy (see
    docs/SECURITY_OPERATIONS.md). Responds 404, not 403, so the admin path
    is not confirmed to unauthorized clients.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        allowed = settings.ADMIN_ALLOWED_IPS
        if allowed and request.path.startswith(f"/{settings.ADMIN_URL}"):
            client_ip = request.META.get("REMOTE_ADDR", "")
            if client_ip not in allowed:
                raise Http404
        return self.get_response(request)


class SecurityHeadersMiddleware:
    """Adds security headers Django doesn't set itself.

    CSP notes: all styling lives in static/css/eve.css, so style-src is
    'self' with no 'unsafe-inline'; there is no inline or external
    JavaScript, so script-src stays 'self'. Product thumbnails come from
    external HTTPS hosts (Saleor CDN), hence img-src https:.
    """

    CSP = "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self' https: data:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ])

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", self.CSP)
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()"
        )
        return response
