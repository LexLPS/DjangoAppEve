"""Seed a staging environment for the load test (loadtest/README.md).

STAGING ONLY. Refuses to run when DJANGO_ENV=prod. With --with-webhook-key
it installs a throwaway RSA public key into the JWKS cache so the load
generator can produce valid webhook signatures; that key is time-bounded by
SALEOR_JWKS_CACHE_SECONDS and must never be installed in production.

Writes a JSON manifest to stdout (or --output) that the locustfile consumes:
users, product slugs, order ids, and the webhook signing key.
"""
import json

from accounts.models import Profile
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from payments.models import Order

LOADTEST_PREFIX = "loadtest_"
LOADTEST_PASSWORD = "LoadTest!2026#eve"
WEBHOOK_KID = "loadtest-key"
LOADTEST_SLUG = "loadtest-product"


def _b64url(value: bytes) -> str:
    import base64
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


class Command(BaseCommand):
    help = "Seed users, orders, and (optionally) a webhook signing key for load testing."

    def add_arguments(self, parser):
        parser.add_argument("--users", type=int, default=20)
        parser.add_argument("--orders-per-user", type=int, default=5,
                            help="Pending orders per user for webhook bursts")
        parser.add_argument("--with-webhook-key", action="store_true",
                            help="Install a throwaway signing key in the JWKS cache")
        parser.add_argument("--output", help="Write the manifest here instead of stdout")
        parser.add_argument("--cleanup", action="store_true",
                            help="Delete all load-test users/orders and exit")

    def handle(self, *args, **options):
        env = getattr(settings, "DJANGO_ENV_NAME", None) or __import__("os").environ.get(
            "DJANGO_ENV", "dev"
        )
        if env == "prod":
            raise CommandError(
                "Refusing to seed load-test data in production (DJANGO_ENV=prod)."
            )

        if options["cleanup"]:
            deleted, _ = User.objects.filter(username__startswith=LOADTEST_PREFIX).delete()
            cache.delete(self._jwks_cache_key())
            from ecommerce.services.mongo_client import products_collection
            products_collection.delete_many({"slug": LOADTEST_SLUG})
            self.stdout.write(self.style.SUCCESS(
                f"Removed load-test data ({deleted} row(s)), the synthetic "
                "product, and the webhook key."
            ))
            return

        users = []
        for index in range(options["users"]):
            username = f"{LOADTEST_PREFIX}{index}"
            user, created = User.objects.get_or_create(
                username=username,
                defaults={"email": f"{username}@loadtest.invalid"},
            )
            if created:
                user.set_password(LOADTEST_PASSWORD)
                user.save(update_fields=["password"])
            # R9: checkout requires a verified address
            Profile.objects.update_or_create(
                user=user, defaults={"email_verified": True},
            )
            users.append(username)

        order_ids = []
        for username in users:
            user = User.objects.get(username=username)
            for n in range(options["orders_per_user"]):
                order_id = f"LOADTEST-{username}-{n}"
                Order.objects.update_or_create(
                    saleor_order_id=order_id,
                    defaults={
                        "user": user,
                        "total_amount": "49.99",
                        "currency": "EUR",
                        "status": Order.Status.PENDING,
                    },
                )
                order_ids.append(order_id)

        # A synthetic product in the Mongo cache makes the cache-hit flow
        # deterministic instead of depending on the staging catalogue.
        # Expires with PRODUCT_CACHE_TTL_SECONDS — re-seed for long runs.
        from ecommerce.services.mongo_client import cache_product
        cache_product({
            "id": "LOADTEST-PRODUCT-1",
            "name": "Load Test Experience",
            "slug": LOADTEST_SLUG,
            "description": "Synthetic product used by the staging load test.",
            "thumbnail": {"url": "https://example.invalid/loadtest.png"},
            "defaultVariant": {"id": "LOADTEST-VARIANT-1"},
            "pricing": {"priceRange": {
                "start": {"gross": {"amount": 49.99, "currency": "EUR"}},
                "stop": {"gross": {"amount": 49.99, "currency": "EUR"}},
            }},
        })

        manifest = {
            "password": LOADTEST_PASSWORD,
            "users": users,
            "order_ids": order_ids,
            "slug": LOADTEST_SLUG,
            "product_cache_ttl": settings.PRODUCT_CACHE_TTL_SECONDS,
            "webhook": None,
        }

        if options["with_webhook_key"]:
            manifest["webhook"] = self._install_webhook_key()

        payload = json.dumps(manifest, indent=2)
        if options["output"]:
            with open(options["output"], "w", encoding="utf-8") as handle:
                handle.write(payload)
            self.stdout.write(self.style.SUCCESS(
                f"Seeded {len(users)} user(s), {len(order_ids)} order(s); "
                f"manifest written to {options['output']}"
            ))
        else:
            self.stdout.write(payload)

    def _jwks_cache_key(self):
        from payments.services.saleor_webhooks import _jwks_url
        return f"saleor:jwks:{_jwks_url()}"

    def _install_webhook_key(self):
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        numbers = private_key.public_key().public_numbers()
        jwks = {"keys": [{
            "kty": "RSA", "use": "sig", "alg": "RS256", "kid": WEBHOOK_KID,
            "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        }]}
        cache.set(self._jwks_cache_key(), jwks, timeout=settings.SALEOR_JWKS_CACHE_SECONDS)
        self.stderr.write(self.style.WARNING(
            "Installed a throwaway webhook signing key in the JWKS cache "
            f"(expires in {settings.SALEOR_JWKS_CACHE_SECONDS}s). Run with "
            "--cleanup afterwards."
        ))
        return {
            "kid": WEBHOOK_KID,
            "private_key_pem": private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ).decode(),
            "expires_in": settings.SALEOR_JWKS_CACHE_SECONDS,
        }
