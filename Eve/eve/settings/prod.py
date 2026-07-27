from decouple import config, Csv
from django.core.exceptions import ImproperlyConfigured

from .base import *

DEBUG = False

# No default here: production must fail loudly if the secret key is missing
SECRET_KEY = config("DJANGO_SECRET_KEY")

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

SECURE_SSL_REDIRECT = True
SESSION_COOKIE_SECURE = True
CSRF_COOKIE_SECURE = True
SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30  # 30 days
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
