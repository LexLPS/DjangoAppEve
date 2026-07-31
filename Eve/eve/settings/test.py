"""Test-runner settings.

The suite must be **hermetic**: `python manage.py test` has to pass on a
fresh clone with no PostgreSQL, MongoDB, Redis, Saleor or SMTP running.
Anything that would reach for an external service is pinned to an in-process
equivalent here, so a contributor's first command succeeds instead of timing
out against services they have not installed yet.

    python manage.py test --settings=eve.settings.test
"""
from .dev import *  # noqa: F403

# In-memory SQLite: no PostgreSQL server, no CREATEDB privilege needed
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Local-memory cache regardless of REDIS_URL in the environment. Without
# this, a .env copied from .env.example points the cache at Redis and every
# test that touches a rate limit, lockout or lease hangs and then errors on
# a machine that has no Redis.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "eve-tests",
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

# Fast hashing keeps the suite quick; PasswordHashingTests overrides this
# to exercise the real Argon2id configuration.
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

# Celery runs inline, so no broker is required
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"

# Never reach a real Saleor instance from the isolated suite. The two live
# integration tests opt in explicitly via SALEOR_INTEGRATION=1.
SALEOR_GRAPHQL_URL = ""

# Point MongoDB at an address that fails fast rather than waiting out the
# 5 s server-selection timeout on machines without it. Tests that exercise
# cart or catalogue behaviour mock the collections; the few that call
# through (resource sampling) assert only that failures degrade politely.
MONGODB = {**MONGODB, "HOST": "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=200"}

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
PUBLIC_BASE_URL = "http://testserver"
SERVER_TIMING_ENABLED = True
