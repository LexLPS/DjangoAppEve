"""Schema and documentation endpoints, served under a strict CSP.

Two constraints had to be met without weakening the site-wide policy:

* **No CDN.** Assets come from drf-spectacular-sidecar via our own
  /static/, so `script-src 'self'` holds.
* **No inline bootstrap script.** The default Swagger view inlines its
  init script, which `script-src 'self'` blocks (the page renders blank).
  The *split* view serves that script as a separate request instead, so
  scripts stay locked down.

Only `style-src` is relaxed, and only on these responses, because both
UIs inject <style> blocks at runtime. The middleware sets the header with
setdefault(), so the per-view value wins here and nowhere else.
"""
from drf_spectacular.utils import extend_schema
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerSplitView,
)

# Same directives as core.middleware.SecurityHeadersMiddleware, plus inline
# styles. The middleware uses setdefault(), so this per-view header wins.
DOCS_CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' https: data:",
    "font-src 'self' data:",
    "connect-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
])


class _RelaxedStyleCSPMixin:
    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        response.headers["Content-Security-Policy"] = DOCS_CSP
        return response


@extend_schema(exclude=True)
class SchemaView(SpectacularAPIView):
    """Machine-readable OpenAPI 3 document (JSON or YAML via Accept)."""


@extend_schema(exclude=True)
class SwaggerView(_RelaxedStyleCSPMixin, SpectacularSwaggerSplitView):
    """Interactive documentation (init script served separately, not inline)."""


@extend_schema(exclude=True)
class RedocView(_RelaxedStyleCSPMixin, SpectacularRedocView):
    """Reference-style documentation."""
