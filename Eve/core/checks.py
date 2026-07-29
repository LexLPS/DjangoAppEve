"""Deploy-time system checks (run by `manage.py check --deploy`, which CI
gates at --fail-level WARNING — a warning here blocks the deploy)."""
from django.conf import settings
from django.core.checks import Tags, Warning, register


@register(Tags.security, deploy=True)
def rate_limit_scale_is_production_safe(app_configs, **kwargs):
    """A load-test scale factor must never reach production: it multiplies
    every brute-force limit (login, registration, contact, admin)."""
    scale = getattr(settings, "RATE_LIMIT_SCALE", 1)
    if scale != 1:
        return [
            Warning(
                f"RATE_LIMIT_SCALE is {scale}, so every per-IP rate limit is "
                f"{scale}x its intended value. This setting exists only for "
                "staging load tests.",
                hint="Unset RATE_LIMIT_SCALE (or set it to 1) outside load testing.",
                id="eve.W002",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def trusted_proxies_configured(app_configs, **kwargs):
    """Threat model R1: behind a reverse proxy with TRUSTED_PROXIES unset,
    every request carries the proxy's address, so per-IP rate limits
    collapse into one shared bucket (an attacker tripping a 429 throttles
    every user) and the admin IP allowlist cannot work."""
    if not settings.TRUSTED_PROXIES:
        return [
            Warning(
                "DJANGO_TRUSTED_PROXIES is not set. Production runs behind a "
                "reverse proxy, so all clients will share the proxy's IP: "
                "per-IP rate limits become one global bucket and the admin "
                "IP allowlist is ineffective.",
                hint=(
                    "Set DJANGO_TRUSTED_PROXIES to the proxy IPs/CIDRs "
                    "(Railway: 100.64.0.0/10). See docs/DEPLOYMENT.md."
                ),
                id="eve.W001",
            )
        ]
    return []
