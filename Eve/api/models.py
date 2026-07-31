"""API credentials.

Access tokens are signed and stateless (see api/tokens.py); only refresh
tokens are persisted, and only as digests. DRF's bundled `authtoken` — a
plaintext, non-expiring bearer credential — was removed in migration
`api.0002` (threat model R11).
"""
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone


class RefreshToken(models.Model):
    """A single-use refresh credential.

    `family` groups every token descended from one login, so detecting the
    reuse of a rotated token lets the whole lineage be revoked at once.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="refresh_tokens"
    )
    # Only the digest is stored; the raw value is returned once
    token_hash = models.CharField(max_length=64, unique=True, db_index=True)
    family = models.CharField(max_length=32, db_index=True)
    label = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)
    # Set when rotated or revoked; a non-null value means "no longer valid"
    used_at = models.DateTimeField(null=True, blank=True)
    revoked_reason = models.CharField(max_length=32, blank=True, default="")

    class Meta:
        indexes = [models.Index(fields=["user", "expires_at"])]

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def __str__(self):
        return f"RefreshToken({self.user.username}, family {self.family[:8]})"


def token_version_for(user) -> int:
    """Version stamped into every access token.

    Incrementing it invalidates all outstanding access tokens for the user
    at once — the revocation lever a stateless token otherwise lacks.
    """
    from accounts.models import Profile

    profile, _ = Profile.objects.get_or_create(user=user)
    return profile.token_version


def revoke_all_tokens(user) -> int:
    """Invalidate every access and refresh token for a user."""
    from accounts.models import Profile

    Profile.objects.filter(user=user).update(token_version=models.F("token_version") + 1)
    return RefreshToken.objects.filter(user=user, used_at__isnull=True).update(
        used_at=timezone.now(), revoked_reason="revoked"
    )
