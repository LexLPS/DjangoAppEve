from decouple import Csv, config
from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

# No default here: production must fail loudly if the secret key is missing
SECRET_KEY = config("DJANGO_SECRET_KEY")
if SECRET_KEY in SECRET_KEY_FALLBACKS:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY_FALLBACKS must not contain the active key.")
if any(len(key) < 50 for key in SECRET_KEY_FALLBACKS):
    raise ImproperlyConfigured("Every fallback signing key must be at least 50 characters.")

# Comma-separated, e.g. DJANGO_ALLOWED_HOSTS=eve.example.com,www.eve.example.com
ALLOWED_HOSTS = config("DJANGO_ALLOWED_HOSTS", cast=Csv())

# Comma-separated full origins, e.g. https://eve.example.com,https://www.eve.example.com
CSRF_TRUSTED_ORIGINS = config("DJANGO_CSRF_TRUSTED_ORIGINS", default="", cast=Csv())

# --- Backends: all required, no fallbacks to dev defaults ---

# PostgreSQL over TLS
DATABASES["default"].update({
    "NAME": config("DB_NAME"),
    "USER": config("DB_USER"),
    "PASSWORD": config("DB_PASSWORD"),
    "HOST": config("DB_HOST"),
    "PORT": config("DB_PORT", default="5432"),
    "OPTIONS": {"sslmode": config("DB_SSLMODE", default="require")},
})
if DATABASES["default"]["PASSWORD"] in ("", "password"):
    raise ImproperlyConfigured("DB_PASSWORD must be set to a real password in production.")

# MongoDB over TLS
_mongodb_uri = config("MONGODB_URI")
if not (
    _mongodb_uri.startswith("mongodb+srv://")
    or "tls=true" in _mongodb_uri.lower()
    or "ssl=true" in _mongodb_uri.lower()
):
    raise ImproperlyConfigured(
        "MONGODB_URI must use TLS in production (mongodb+srv:// or ?tls=true)."
    )
MONGODB = {
    "HOST": _mongodb_uri,
    "DB_NAME": config("MONGODB_DB_NAME"),
}

# Saleor over HTTPS
SALEOR_GRAPHQL_URL = config("SALEOR_GRAPHQL_URL")
if not SALEOR_GRAPHQL_URL.startswith("https://"):
    raise ImproperlyConfigured("SALEOR_GRAPHQL_URL must be an https:// URL in production.")

# Redis is mandatory in production: rate limits, lockouts, and cached
# sessions must be shared across workers, not per-process
REDIS_URL = config("REDIS_URL")
if not REDIS_URL.startswith(("redis://", "rediss://")):
    raise ImproperlyConfigured("REDIS_URL must be a redis:// or rediss:// URL.")
CELERY_BROKER_URL = config("CELERY_BROKER_URL")
if not CELERY_BROKER_URL.startswith(("redis://", "rediss://")):
    raise ImproperlyConfigured("CELERY_BROKER_URL must be a redis:// or rediss:// URL.")
from core.cache import SafeJSONSerializer  # noqa: E402

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_URL,
        "OPTIONS": {
            # JSON instead of pickle (threat model R2)
            "serializer": SafeJSONSerializer(),
            "max_connections": config("REDIS_MAX_CONNECTIONS", default=50, cast=int),
            "socket_connect_timeout": 2,
            "socket_timeout": 2,
            "retry_on_timeout": True,
        },
        "TIMEOUT": 300,
    }
}

# An explicit override, when supplied, must remain HTTPS. Otherwise the JWKS
# endpoint is safely derived from the already HTTPS-only GraphQL origin.
if SALEOR_JWKS_URL and not SALEOR_JWKS_URL.startswith("https://"):
    raise ImproperlyConfigured("SALEOR_JWKS_URL must be an https:// URL in production.")

# Admin MFA is on by default in production
ADMIN_REQUIRE_MFA = config("DJANGO_ADMIN_REQUIRE_MFA", default=True, cast=bool)

# Structured JSON logs by default in production
if config("LOG_FORMAT", default="json") == "json":
    LOGGING["handlers"]["console"]["formatter"] = "json"

# Real SMTP for password reset / verification mail — fail loudly if absent
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = config("EMAIL_HOST")
EMAIL_PORT = config("EMAIL_PORT", default=587, cast=int)
EMAIL_HOST_USER = config("EMAIL_HOST_USER", default="")
EMAIL_HOST_PASSWORD = config("EMAIL_HOST_PASSWORD", default="")
EMAIL_USE_TLS = config("EMAIL_USE_TLS", default=True, cast=bool)
DEFAULT_FROM_EMAIL = config("DEFAULT_FROM_EMAIL")
PUBLIC_BASE_URL = config("PUBLIC_BASE_URL")
if not PUBLIC_BASE_URL.startswith("https://"):
    raise ImproperlyConfigured("PUBLIC_BASE_URL must be an https:// URL in production.")

# TLS terminates at the reverse proxy; trust its X-Forwarded-Proto so
# request.is_secure() is correct. Only safe because the proxy overwrites
# the header on every request (see docs/DEPLOYMENT.md).
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30  # 30 days
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
