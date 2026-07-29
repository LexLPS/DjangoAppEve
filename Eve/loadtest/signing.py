"""Saleor webhook signing for the load generator.

Kept free of locust imports so the test suite can verify that signatures
produced here are accepted by payments.services.saleor_webhooks - if Saleor's
format or our verifier changes, that test fails instead of the load test
silently measuring 401s.
"""
import base64
import json

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class SaleorSigner:
    """Produces RFC 7797 detached JWS signatures exactly like Saleor does."""

    def __init__(self, webhook: dict):
        self.kid = webhook["kid"]
        self.key = serialization.load_pem_private_key(
            webhook["private_key_pem"].encode(), password=None
        )

    def sign(self, payload: dict):
        """Return (raw_body, Saleor-Signature header value)."""
        body = json.dumps(payload).encode()
        protected = b64url(json.dumps(
            {"alg": "RS256", "kid": self.kid, "b64": False, "crit": ["b64"]},
            separators=(",", ":"),
        ).encode())
        signature = self.key.sign(
            protected.encode() + b"." + body, padding.PKCS1v15(), hashes.SHA256()
        )
        return body, f"{protected}..{b64url(signature)}"
