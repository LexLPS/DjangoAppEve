"""Short-lived signed access tokens with rotating refresh tokens.

Design, and why:

* **Access tokens are signed, not stored.** Django's `TimestampSigner`
  produces a tamper-evident token carrying the user id and a version
  counter; the server keeps nothing, so a database disclosure yields no
  usable access credential. They live for `API_ACCESS_TOKEN_TTL_SECONDS`
  (15 minutes), which bounds the damage of a leaked one without needing
  per-request revocation lookups.
* **Refresh tokens are stored hashed and rotate on every use.** Each
  refresh returns a new refresh token and retires the presented one.
* **Reuse of a retired refresh token revokes the whole family.** That is
  the standard theft signal: if both the legitimate client and an attacker
  hold the same refresh token, the second use is evidence of compromise, so
  the session is ended rather than silently extended.
* **`token_version` on the user's profile is the revocation lever.**
  Bumping it invalidates every outstanding access token immediately, which
  a signed token cannot otherwise offer.
"""
import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.utils import timezone

logger = logging.getLogger(__name__)

ACCESS_SALT = "api.access-token"
REFRESH_BYTES = 32


class InvalidToken(Exception):
    """Presented credential is missing, malformed, expired or revoked."""


def hash_refresh_token(raw_token: str) -> str:
    """Digest a refresh token. A plain SHA-256 is correct: the value is 256
    bits of server-generated randomness, so there is nothing to brute-force,
    and a password hash would only slow every refresh down."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


def issue_access_token(user) -> tuple[str, int]:
    """Return (token, expires_in_seconds)."""
    from api.models import token_version_for

    payload = {"uid": user.pk, "ver": token_version_for(user)}
    return (
        signing.dumps(payload, salt=ACCESS_SALT),
        settings.API_ACCESS_TOKEN_TTL_SECONDS,
    )


def read_access_token(raw_token: str) -> User:
    """Verify signature, age and version; return the user or raise."""
    from api.models import token_version_for

    try:
        payload = signing.loads(
            raw_token,
            salt=ACCESS_SALT,
            max_age=settings.API_ACCESS_TOKEN_TTL_SECONDS,
        )
    except signing.SignatureExpired:
        raise InvalidToken("expired") from None
    except signing.BadSignature:
        raise InvalidToken("bad signature") from None

    user = User.objects.filter(pk=payload.get("uid"), is_active=True).first()
    if user is None:
        raise InvalidToken("unknown or inactive user")
    # A version bump revokes every outstanding access token at once
    if payload.get("ver") != token_version_for(user):
        raise InvalidToken("revoked")
    return user


def issue_refresh_token(user, *, family=None, user_agent=""):
    """Create a refresh token, returning (instance, raw_token)."""
    from api.models import RefreshToken

    raw_token = secrets.token_urlsafe(REFRESH_BYTES)
    instance = RefreshToken.objects.create(
        user=user,
        token_hash=hash_refresh_token(raw_token),
        family=family or secrets.token_hex(16),
        label=user_agent[:64],
        expires_at=timezone.now()
        + timedelta(days=settings.API_REFRESH_TOKEN_TTL_DAYS),
    )
    return instance, raw_token


def rotate_refresh_token(raw_token: str):
    """Exchange a refresh token for a new pair.

    Returns (user, new_raw_refresh). Raises InvalidToken when the token is
    unknown, expired, or already used — the last case also revokes the
    family, because a retired token in circulation means it was captured.
    """
    from api.models import RefreshToken

    presented = RefreshToken.objects.select_related("user").filter(
        token_hash=hash_refresh_token(raw_token)
    ).first()
    if presented is None:
        raise InvalidToken("unknown refresh token")

    if presented.used_at is not None:
        # Replay of a rotated token: assume theft and end the session
        revoked = RefreshToken.objects.filter(
            family=presented.family, used_at__isnull=True
        ).update(used_at=timezone.now(), revoked_reason="reuse_detected")
        logger.error(
            "Refresh token reuse detected; family revoked",
            extra={
                "event": "refresh_token_reuse",
                "family": presented.family,
                "revoked": revoked,
            },
        )
        raise InvalidToken("refresh token already used")

    if presented.is_expired or not presented.user.is_active:
        raise InvalidToken("expired refresh token")

    presented.used_at = timezone.now()
    presented.revoked_reason = "rotated"
    presented.save(update_fields=["used_at", "revoked_reason"])

    _, new_raw = issue_refresh_token(
        presented.user, family=presented.family, user_agent=presented.label
    )
    return presented.user, new_raw
