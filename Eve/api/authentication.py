"""Bearer authentication for short-lived signed access tokens.

Wire format is unchanged (`Authorization: Token <access-token>`), but the
credential is now a signed, 15-minute token rather than a database row that
lives for weeks. Nothing is looked up per request except the user, and a
`token_version` bump revokes every outstanding token at once.
"""
import logging

from drf_spectacular.extensions import OpenApiAuthenticationExtension
from rest_framework import authentication, exceptions

from .tokens import InvalidToken, read_access_token

logger = logging.getLogger(__name__)

KEYWORD = "Token"


class AccessTokenAuthentication(authentication.BaseAuthentication):
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

        try:
            user = read_access_token(raw_token)
        except InvalidToken as exc:
            # One message for every failure mode: never reveal whether a
            # token existed, expired, or was revoked
            logger.info(
                "Access token rejected",
                extra={"event": "access_token_rejected", "reason": str(exc)},
            )
            raise exceptions.AuthenticationFailed("Invalid or expired token.") from None

        return (user, raw_token)

    def authenticate_header(self, request):
        # Drives 401 (rather than 403) on missing/invalid credentials
        return KEYWORD


class AccessTokenScheme(OpenApiAuthenticationExtension):
    """Teaches drf-spectacular how to document this scheme; without it the
    generator omits the security scheme from the published contract."""

    target_class = "api.authentication.AccessTokenAuthentication"
    name = "TokenAuth"

    def get_security_definition(self, auto_schema):
        return {
            "type": "apiKey",
            "in": "header",
            "name": "Authorization",
            "description": (
                "Short-lived access token from `/api/v1/auth/token/`, sent as "
                "`Authorization: Token <access>`. Expires in 15 minutes; use "
                "`/api/v1/auth/token/refresh/` to rotate."
            ),
        }
