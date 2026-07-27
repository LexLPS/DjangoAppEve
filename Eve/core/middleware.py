from django.conf import settings
from django.http import Http404


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
