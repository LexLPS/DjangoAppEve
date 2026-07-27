from django.conf import settings
from django.http import Http404


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

    CSP notes: templates use inline style="" attributes, so style-src needs
    'unsafe-inline'; there is no inline or external JavaScript, so script-src
    stays 'self'. Product thumbnails come from external HTTPS hosts (Saleor
    CDN), hence img-src https:.
    """

    CSP = "; ".join([
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
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
