"""API credentials.

DRF's bundled `authtoken` stores the token value in plaintext and never
expires it, so any read of the database — a backup, a log, a SQL injection
elsewhere — yields working credentials indefinitely (threat model R11).
This model stores only a SHA-256 digest and carries an expiry, so a
database disclosure yields digests that cannot be replayed.
"""
import hashlib
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

TOKEN_BYTES = 32  # 256 bits of entropy


def hash_token(raw_token: str) -> str:
    """Digest a bearer token.

    A plain SHA-256 is correct here and a password hash would not be: the
    token is 256 bits of server-generated randomness, so there is nothing
    to brute-force, and per-request logins must stay cheap.
    """
    return hashlib.sha256(raw_token.encode()).hexdigest()


class ApiToken(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="api_tokens")
    # Only the digest is stored; the raw value is shown once at creation
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    last_used_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [models.Index(fields=["user", "expires_at"])]

    @classmethod
    def issue(cls, user, label: str = ""):
        """Create a token and return (instance, raw_token).

        The raw value is never persisted and cannot be recovered later.
        """
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        instance = cls.objects.create(
            user=user,
            token_hash=hash_token(raw_token),
            label=label[:64],
            expires_at=timezone.now() + timedelta(days=settings.API_TOKEN_TTL_DAYS),
        )
        return instance, raw_token

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"ApiToken({self.user.username}, expires {self.expires_at:%Y-%m-%d})"
