"""Staging: identical hardening to production (inherits every fail-closed
check), pointed at staging backends via its own environment variables.
Differences from production are deliberate and minimal."""
from .prod import *  # noqa: F403

# Staging traffic is internal; the domain must never enter the browser
# preload list. Deliberate, so silence the corresponding deploy check.
SECURE_HSTS_PRELOAD = False
SILENCED_SYSTEM_CHECKS = ["security.W021"]

# Cheaper error sampling is fine outside production
SENTRY_ENVIRONMENT = "staging"
