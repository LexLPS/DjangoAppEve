"""
Settings selection fails closed: production settings load unless the
environment explicitly opts into development with DJANGO_ENV=dev.
"""
from decouple import config
from django.core.exceptions import ImproperlyConfigured

_env = config("DJANGO_ENV", default="prod")

if _env == "dev":
    from .dev import *
elif _env == "staging":
    from .staging import *
elif _env == "prod":
    from .prod import *
else:
    raise ImproperlyConfigured(
        f"Unknown DJANGO_ENV value {_env!r}; expected 'dev', 'staging', or 'prod'."
    )
