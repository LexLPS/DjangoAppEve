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


@register(Tags.security, deploy=True)
def cache_and_broker_are_isolated(app_configs, **kwargs):
    """The durable Celery broker must not inherit cache eviction behavior."""
    cache_url = getattr(settings, "REDIS_URL", "")
    broker_url = getattr(settings, "CELERY_BROKER_URL", "")
    if cache_url and cache_url == broker_url:
        return [
            Warning(
                "REDIS_URL and CELERY_BROKER_URL point to the same service. "
                "Cache eviction can then discard queued payment work.",
                hint="Use a separate persistent no-eviction Redis broker.",
                id="eve.W003",
            )
        ]
    return []


@register(Tags.security, deploy=True)
def signing_key_fallbacks_are_safe(app_configs, **kwargs):
    active = getattr(settings, "SECRET_KEY", "")
    fallbacks = getattr(settings, "SECRET_KEY_FALLBACKS", [])
    if active and active in fallbacks:
        return [
            Warning(
                "The active Django signing key is duplicated in SECRET_KEY_FALLBACKS.",
                hint="Keep only retired keys in DJANGO_SECRET_KEY_FALLBACKS.",
                id="eve.W004",
            )
        ]
    return []
