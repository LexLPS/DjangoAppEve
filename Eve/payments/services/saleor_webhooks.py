"""Verification for Saleor's detached RS256 webhook signatures."""

import base64
import json
from urllib.parse import urlparse

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.conf import settings
from django.core.cache import cache


class WebhookSignatureError(ValueError):
    """Raised when a webhook cannot be authenticated."""


def _b64url_decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as exc:
        raise WebhookSignatureError("invalid base64url value") from exc


def _jwks_url() -> str:
    explicit = getattr(settings, "SALEOR_JWKS_URL", "")
    if explicit:
        return explicit

    graphql_url = getattr(settings, "SALEOR_GRAPHQL_URL", "")
    parsed = urlparse(graphql_url)
    if not parsed.scheme or not parsed.netloc:
        raise WebhookSignatureError("Saleor JWKS URL is not configured")
    return f"{parsed.scheme}://{parsed.netloc}/.well-known/jwks.json"


def _load_jwks(*, force_refresh: bool = False) -> dict:
    url = _jwks_url()
    parsed = urlparse(url)
    if parsed.scheme not in ({"https"} if not settings.DEBUG else {"http", "https"}):
        raise WebhookSignatureError("Saleor JWKS URL must use HTTPS")

    cache_key = f"saleor:jwks:{url}"
    if not force_refresh:
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            return cached

    try:
        response = requests.get(url, timeout=(2, 5), allow_redirects=False)
        response.raise_for_status()
        jwks = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise WebhookSignatureError("unable to retrieve Saleor signing keys") from exc

    if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
        raise WebhookSignatureError("invalid Saleor JWKS document")
    cache.set(cache_key, jwks, timeout=settings.SALEOR_JWKS_CACHE_SECONDS)
    return jwks


def _public_key(jwks: dict, kid: str):
    matches = [
        key for key in jwks["keys"]
        if key.get("kid") == kid
        and key.get("kty") == "RSA"
        and key.get("alg", "RS256") == "RS256"
        and key.get("use", "sig") == "sig"
    ]
    if len(matches) != 1:
        raise WebhookSignatureError("signing key not found")
    try:
        modulus = int.from_bytes(_b64url_decode(matches[0]["n"]), "big")
        exponent = int.from_bytes(_b64url_decode(matches[0]["e"]), "big")
        return rsa.RSAPublicNumbers(exponent, modulus).public_key()
    except (KeyError, TypeError, ValueError) as exc:
        raise WebhookSignatureError("invalid RSA signing key") from exc


def verify_saleor_signature(raw_body: bytes, signature: str) -> None:
    """Verify a compact detached JWS from the ``Saleor-Signature`` header."""
    if not signature or len(signature) > 8192:
        raise WebhookSignatureError("missing or oversized signature")
    try:
        protected_b64, detached_payload, signature_b64 = signature.split(".")
    except ValueError as exc:
        raise WebhookSignatureError("malformed detached JWS") from exc
    if detached_payload != "":
        raise WebhookSignatureError("JWS payload must be detached")

    try:
        protected = json.loads(_b64url_decode(protected_b64))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise WebhookSignatureError("invalid protected header") from exc
    if not isinstance(protected, dict) or protected.get("alg") != "RS256":
        raise WebhookSignatureError("unsupported signature algorithm")
    if protected.get("b64", True) is not True or "crit" in protected:
        raise WebhookSignatureError("unsupported JWS protected header")
    kid = protected.get("kid")
    if not isinstance(kid, str) or not kid or len(kid) > 256:
        raise WebhookSignatureError("missing signing key id")

    encoded_payload = base64.urlsafe_b64encode(raw_body).rstrip(b"=")
    signed_data = protected_b64.encode("ascii") + b"." + encoded_payload
    signature_bytes = _b64url_decode(signature_b64)

    for force_refresh in (False, True):
        try:
            key = _public_key(_load_jwks(force_refresh=force_refresh), kid)
            key.verify(signature_bytes, signed_data, padding.PKCS1v15(), hashes.SHA256())
            return
        except InvalidSignature:
            if force_refresh:
                raise WebhookSignatureError("invalid webhook signature") from None
        except WebhookSignatureError:
            if force_refresh:
                raise
    raise WebhookSignatureError("invalid webhook signature")
