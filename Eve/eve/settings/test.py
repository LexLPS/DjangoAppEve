"""Test-runner settings: identical to dev except the relational database is
in-memory SQLite (the local Postgres role has no CREATEDB privilege) and
password hashing is fast. Run with:

    python manage.py test --settings=eve.settings.test
"""
from .dev import *

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
