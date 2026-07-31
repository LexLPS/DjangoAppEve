"""Bearer-token authentication backed by hashed, expiring tokens.

Wire-compatible with DRF's `TokenAuthentication` (`Authorization: Token
<key>`), so existing clients need no change, but the stored credential is a
digest with an expiry rather than a plaintext value that lives forever.
"""
import logging
from datetime import timedelta

from django.utils import timezone
from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from .models import ApiToken, hash_token

logger = logging.getLogger(__name__)

KEYWORD = "Token"
# Avoid a database write on every authenticated request
LAST_USED_RESOLUTION = timedelta(hours=1)


class HashedTokenAuthentication(authentication.BaseAuthentication):
    def authenticate(self, request):
        header = authentication.get_authorization_header(request).split()
        if not header or header[0].lower() != KEYWORD.lower().encode():
            return None  # not our scheme; let the next authenticator try
        if len(header) != 2:
            raise exceptions.AuthenticationFailed("Malformed Authorization header.")

        try:
            raw_token = header[1].decode()
        except UnicodeError:
            raise exceptions.AuthenticationFailed("Malformed token.") from None

        token = (
            ApiToken.objects.select_related("user")
            .filter(token_hash=hash_token(raw_token))
            .first()
        )
        # Same message either way: never reveal whether a token exists
        if token is None or token.is_expired:
            if token is not None:
                logger.info(
                    "Expired API token presented",
                    extra={"event": "api_token_expired", "token_id": token.pk},
                )
            raise exceptions.AuthenticationFailed("Invalid or expired token.")

        if not token.user.is_active:
            raise exceptions.AuthenticationFailed("Invalid or expired token.")

        now = timezone.now()
        if token.last_used_at is None or now - token.last_used_at > LAST_USED_RESOLUTION:
            ApiToken.objects.filter(pk=token.pk).update(last_used_at=now)

        return (token.user, token)

    def authenticate_header(self, request):
        # Drives the 401 (rather than 403) on missing/!invalid credentials
        return KEYWORD


class HashedTokenScheme(OpenApiAuthenticationExtension):
    """Teaches drf-spectacular how to document this scheme.

    Without it the generator emits 'could not resolve authenticator' and
    silently omits the security scheme, so the published contract would not
    tell clients how to authenticate.
    """

    target_class = "api.authentication.HashedTokenAuthentication"
    name = "TokenAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": (
                "Bearer token obtained from `/api/v1/auth/token/`, sent as "
                "`Authorization: Token <key>`. Tokens expire and are stored "
                "only as digests."
            ),
        }
