import base64
import json
import os
from unittest import skipUnless
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Order, WebhookEvent
from .services.saleor_webhooks import WebhookSignatureError

TEST_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
ATTACKER_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
TEST_KID = "saleor-test-key"


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _jwk(private_key=TEST_PRIVATE_KEY, kid=TEST_KID):
    numbers = private_key.public_key().public_numbers()
    return {
        "kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid,
        "n": _b64url(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
        "e": _b64url(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
    }


TEST_JWKS = {"keys": [_jwk()]}


def _signed(payload: dict, private_key=TEST_PRIVATE_KEY, kid=TEST_KID):
    body = json.dumps(payload).encode()
    protected = _b64url(json.dumps(
        {"alg": "RS256", "kid": kid, "b64": False, "crit": ["b64"]},
        separators=(",", ":"),
    ).encode())
    signature = private_key.sign(
        protected.encode() + b"." + body, padding.PKCS1v15(), hashes.SHA256()
    )
    return body, f"{protected}..{_b64url(signature)}"


class CheckoutAuthorizationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")

    def test_checkout_requires_login(self):
        response = self.client.get(reverse("checkout"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)

    def test_anonymous_post_cannot_create_orders(self):
        response = self.client.post(reverse("checkout"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.objects.count(), 0)

    def test_checkout_disabled_by_default_post_creates_no_order(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("checkout"))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(Order.objects.count(), 0)

    def test_history_requires_login(self):
        response = self.client.get(reverse("payment_history"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


@override_settings(CHECKOUT_ENABLED=True)
class CheckoutFlowTests(TestCase):
    """Server-side checkout: Saleor totals only, idempotent order creation."""

    def setUp(self):
        from accounts.models import Profile
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        Profile.objects.create(user=self.user, email_verified=True)
        self.client.force_login(self.user)
        self.cart = {
            "user_id": self.user.id,
            "items": [{
                "product_id": "P1", "slug": "eve-horizon", "name": "Eve Horizon",
                "variant_id": "V1", "price_amount": 1.00,  # stale client-visible price
                "price_currency": "EUR", "quantity": 2,
            }],
        }

    def _post_checkout(self):
        with patch("payments.views.get_cart", return_value=self.cart), \
             patch("payments.views.clear_cart"), \
             patch("payments.views.create_checkout",
                   return_value={"checkout_id": "CHK1", "total_amount": 99.98,
                                 "total_currency": "EUR"}), \
             patch("payments.views.complete_checkout",
                   return_value={"order_id": "ORD1", "total_amount": 99.98,
                                 "total_currency": "EUR"}):
            self.client.get(reverse("checkout"))  # sets the idempotency key
            return self.client.post(reverse("checkout"))

    def test_order_uses_saleor_calculated_total_not_cart_price(self):
        response = self._post_checkout()
        self.assertRedirects(response, reverse("payment_history"),
                             fetch_redirect_response=False)
        order = Order.objects.get()
        self.assertEqual(float(order.total_amount), 99.98)  # not 2 * 1.00
        self.assertEqual(order.status, Order.Status.PENDING)
        self.assertEqual(order.saleor_order_id, "ORD1")

    def test_duplicate_submit_with_same_key_creates_one_order(self):
        self._post_checkout()
        key_used = Order.objects.get().idempotency_key
        # Simulate a double-submit: same idempotency key, second POST
        session = self.client.session
        session["checkout_idempotency_key"] = key_used
        session.save()
        with patch("payments.views.get_cart", return_value=self.cart), \
             patch("payments.views.clear_cart"), \
             patch("payments.views.create_checkout") as create_mock, \
             patch("payments.views.complete_checkout"):
            response = self.client.post(reverse("checkout"))
        self.assertEqual(Order.objects.count(), 1)
        create_mock.assert_not_called()  # no second Saleor checkout
        self.assertRedirects(response, reverse("payment_history"),
                             fetch_redirect_response=False)

    def test_post_without_idempotency_key_redirects_without_order(self):
        with patch("payments.views.get_cart", return_value=self.cart):
            response = self.client.post(reverse("checkout"))
        self.assertEqual(Order.objects.count(), 0)
        self.assertRedirects(response, reverse("checkout"),
                             fetch_redirect_response=False)

    def test_unverified_email_cannot_place_orders(self):
        from accounts.models import Profile
        Profile.objects.filter(user=self.user).update(email_verified=False)
        with patch("payments.views.get_cart", return_value=self.cart), \
             patch("payments.views.create_checkout") as create_mock:
            response = self.client.post(reverse("checkout"))
        self.assertRedirects(response, reverse("profile"),
                             fetch_redirect_response=False)
        self.assertEqual(Order.objects.count(), 0)
        create_mock.assert_not_called()


@override_settings(SALEOR_GRAPHQL_URL="https://saleor.example.com/graphql/")
class WebhookTests(TestCase):
    def setUp(self):
        cache.clear()
        jwks_patch = patch(
            "payments.services.saleor_webhooks._load_jwks", return_value=TEST_JWKS
        )
        jwks_patch.start()
        self.addCleanup(jwks_patch.stop)
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.order = Order.objects.create(
            user=self.user, saleor_order_id="ORD1",
            total_amount="99.98", currency="EUR", status=Order.Status.PENDING,
        )
        self.url = reverse("saleor_webhook")

    def _post(self, payload, signature=None, private_key=TEST_PRIVATE_KEY):
        body, computed = _signed(payload, private_key)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                self.url, data=body, content_type="application/json",
                headers={"Saleor-Signature": signature or computed},
            )

    def test_valid_signature_marks_order_paid(self):
        response = self._post({"__typename": "OrderFullyPaid",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 202)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_invalid_signature_rejected(self):
        response = self._post({"__typename": "OrderFullyPaid",
                               "order": {"id": "ORD1"}},
                              signature="0" * 64)
        self.assertEqual(response.status_code, 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_wrong_secret_rejected(self):
        body, signature = _signed(
            {"__typename": "OrderFullyPaid", "order": {"id": "ORD1"}},
            private_key=ATTACKER_PRIVATE_KEY,
        )
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            headers={"Saleor-Signature": signature},
        )
        self.assertEqual(response.status_code, 401)

    def test_unavailable_jwks_rejects_everything(self):
        with patch(
            "payments.services.saleor_webhooks._load_jwks",
            side_effect=WebhookSignatureError("unavailable"),
        ):
            response = self._post({"__typename": "OrderFullyPaid",
                                   "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 401)

    def test_redelivery_is_idempotent(self):
        payload = {"__typename": "OrderFullyPaid", "order": {"id": "ORD1"}}
        self._post(payload)
        response = self._post(payload)  # same event again
        self.assertEqual(response.status_code, 202)
        self.assertTrue(response.json()["duplicate"])
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_invalid_state_transition_refused(self):
        self.order.status = Order.Status.REFUNDED
        self.order.save()
        response = self._post({"__typename": "OrderFullyPaid",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(WebhookEvent.objects.get().status, WebhookEvent.Status.REJECTED)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REFUNDED)

    def test_refund_flow(self):
        self.order.status = Order.Status.PAID
        self.order.save()
        response = self._post({"__typename": "OrderRefunded",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 202)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REFUNDED)

    def test_unknown_order_acknowledged_without_changes(self):
        response = self._post({"__typename": "OrderFullyPaid",
                               "order": {"id": "GHOST"}})
        self.assertEqual(response.status_code, 202)
        event = WebhookEvent.objects.get()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertEqual(event.last_error, "unknown_order")

    def test_malformed_payload_rejected(self):
        body = b"not json"
        protected = _b64url(json.dumps({
            "alg": "RS256", "kid": TEST_KID, "b64": False, "crit": ["b64"],
        }).encode())
        signed = TEST_PRIVATE_KEY.sign(
            protected.encode() + b"." + body, padding.PKCS1v15(), hashes.SHA256()
        )
        signature = f"{protected}..{_b64url(signed)}"
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            headers={"Saleor-Signature": signature},
        )
        self.assertEqual(response.status_code, 400)

    def test_broker_failure_leaves_durable_pending_event(self):
        payload = {"__typename": "OrderFullyPaid", "order": {"id": "ORD1"}}
        body, signature = _signed(payload)
        with patch(
            "payments.tasks.process_webhook_event.delay",
            side_effect=ConnectionError("broker unavailable"),
        ), self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                self.url,
                data=body,
                content_type="application/json",
                headers={"Saleor-Signature": signature},
            )
        self.assertEqual(response.status_code, 202)
        event = WebhookEvent.objects.get()
        self.assertEqual(event.status, WebhookEvent.Status.PENDING)
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_inbox_stores_only_minimum_safe_payload(self):
        self._post({
            "__typename": "OrderFullyPaid",
            "order": {"id": "ORD1", "userEmail": "private@example.com"},
        })
        payload = WebhookEvent.objects.get().payload
        self.assertEqual(
            payload,
            {"__typename": "OrderFullyPaid", "order": {"id": "ORD1"}},
        )

    def test_recovery_republishes_pending_events(self):
        from payments.tasks import recover_pending_webhooks

        event = WebhookEvent.objects.create(
            fingerprint="a" * 64,
            event_type="OrderFullyPaid",
            saleor_order_id="ORD1",
            payload={"__typename": "OrderFullyPaid", "order": {"id": "ORD1"}},
        )
        with patch("payments.tasks.process_webhook_event.delay") as delay:
            queued = recover_pending_webhooks.run()
        self.assertEqual(queued, 1)
        delay.assert_called_once_with(event.pk)


class OrderOwnershipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "S3curePass!x")
        Order.objects.create(
            user=self.alice, saleor_order_id="SO_ALICE_1",
            total_amount="10.00", currency="EUR", status="pending",
        )
        Order.objects.create(
            user=self.bob, saleor_order_id="SO_BOB_1",
            total_amount="20.00", currency="EUR", status="pending",
        )

    def test_history_shows_only_own_orders(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("payment_history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SO_ALICE_1")
        self.assertNotContains(response, "SO_BOB_1")


@override_settings(
    CHECKOUT_ENABLED=True,
    SALEOR_GRAPHQL_URL="https://saleor.example.com/graphql/",
)
class EndToEndOrderJourneyTests(TestCase):
    """Full journey with Saleor and Mongo mocked at the service boundary:
    register -> add to cart -> checkout -> webhook payment -> paid history,
    plus the refund and duplicate-delivery paths."""

    def setUp(self):
        cache.clear()
        jwks_patch = patch(
            "payments.services.saleor_webhooks._load_jwks", return_value=TEST_JWKS
        )
        jwks_patch.start()
        self.addCleanup(jwks_patch.stop)

    def _register_and_login(self):
        self.client.post(reverse("register"), {
            "username": "carol", "email": "carol@example.com",
            "password1": "S3curePass!x", "password2": "S3curePass!x",
        })  # register logs the user in

    def test_full_purchase_refund_and_redelivery(self):
        import re

        from django.core import mail

        self._register_and_login()
        user = User.objects.get(username="carol")

        # Verify the email address (required before checkout, R9)
        link = re.search(r"(/accounts/verify-email/[^\s]+)", mail.outbox[0].body)
        self.assertIsNotNone(link)
        self.assertEqual(self.client.get(link.group(1)).status_code, 200)

        product = {
            "id": "P1", "name": "Eve Horizon", "slug": "eve-horizon",
            "defaultVariant": {"id": "V1"},
            "pricing": {"priceRange": {"start": {"gross": {
                "amount": 49.99, "currency": "EUR"}}}},
        }
        cart = {"user_id": user.id, "items": [{
            "product_id": "P1", "slug": "eve-horizon", "name": "Eve Horizon",
            "variant_id": "V1", "price_amount": 49.99,
            "price_currency": "EUR", "quantity": 1,
        }]}

        # Add to cart (product lookup and cart storage mocked)
        with patch("ecommerce.views.get_cached_product", return_value=product), \
             patch("ecommerce.views.add_to_cart") as add_mock:
            response = self.client.post(
                reverse("add_to_cart", args=["eve-horizon"]), {"quantity": "1"})
            self.assertRedirects(response, reverse("cart"),
                                 fetch_redirect_response=False)
            add_mock.assert_called_once()

        # Checkout with Saleor-calculated total
        with patch("payments.views.get_cart", return_value=cart), \
             patch("payments.views.clear_cart") as clear_mock, \
             patch("payments.views.create_checkout",
                   return_value={"checkout_id": "CHK1", "total_amount": 49.99,
                                 "total_currency": "EUR"}), \
             patch("payments.views.complete_checkout",
                   return_value={"order_id": "ORD-E2E", "total_amount": 49.99,
                                 "total_currency": "EUR"}):
            self.client.get(reverse("checkout"))
            response = self.client.post(reverse("checkout"))
            self.assertRedirects(response, reverse("payment_history"),
                                 fetch_redirect_response=False)
            clear_mock.assert_called_once_with(user.id)

        order = Order.objects.get(saleor_order_id="ORD-E2E")
        self.assertEqual(order.status, Order.Status.PENDING)

        # Payment webhook arrives (twice — duplicate delivery must be a no-op)
        payload = {"__typename": "OrderFullyPaid", "order": {"id": "ORD-E2E"}}
        body, signature = _signed(payload)
        for _ in range(2):
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(
                    reverse("saleor_webhook"), data=body,
                    content_type="application/json",
                    headers={"Saleor-Signature": signature},
                )
            self.assertEqual(response.status_code, 202)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(Order.objects.count(), 1)

        # History shows the paid order
        response = self.client.get(reverse("payment_history"))
        self.assertContains(response, "ORD-E2E")
        self.assertContains(response, "Paid")

        # Refund webhook completes the lifecycle
        body, signature = _signed(
            {"__typename": "OrderRefunded", "order": {"id": "ORD-E2E"}})
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("saleor_webhook"), data=body,
                content_type="application/json",
                headers={"Saleor-Signature": signature},
            )
        self.assertEqual(response.status_code, 202)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)


@override_settings(SALEOR_GRAPHQL_URL="https://saleor.example.com/graphql/")
class LoadTestSignerCompatibilityTests(TestCase):
    """The load generator must sign exactly the way the app verifies —
    otherwise a load test silently measures 401s instead of throughput."""

    def _signer_module(self):
        import importlib.util
        from pathlib import Path
        path = Path(__file__).resolve().parent.parent / "loadtest" / "signing.py"
        spec = importlib.util.spec_from_file_location("loadtest_signing", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_generator_signatures_pass_production_verification(self):
        from cryptography.hazmat.primitives import serialization

        from .services.saleor_webhooks import verify_saleor_signature

        pem = TEST_PRIVATE_KEY.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        signer = self._signer_module().SaleorSigner(
            {"kid": TEST_KID, "private_key_pem": pem}
        )
        body, signature = signer.sign(
            {"__typename": "OrderFullyPaid", "order": {"id": "ORD1"}}
        )
        with patch("payments.services.saleor_webhooks._load_jwks",
                   return_value=TEST_JWKS):
            verify_saleor_signature(body, signature)  # must not raise

    def test_tampered_body_is_rejected(self):
        from cryptography.hazmat.primitives import serialization

        from .services.saleor_webhooks import verify_saleor_signature

        pem = TEST_PRIVATE_KEY.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        signer = self._signer_module().SaleorSigner(
            {"kid": TEST_KID, "private_key_pem": pem}
        )
        _, signature = signer.sign({"__typename": "OrderFullyPaid",
                                    "order": {"id": "ORD1"}})
        with patch("payments.services.saleor_webhooks._load_jwks",
                   return_value=TEST_JWKS), \
             self.assertRaises(WebhookSignatureError):
            verify_saleor_signature(b'{"__typename":"OrderFullyPaid"}', signature)


class ReconcileOrdersTests(TestCase):
    """R4: orders Saleor knows about but Eve doesn't must be surfaced."""

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        Order.objects.create(
            user=self.user, saleor_order_id="ORD-KNOWN",
            total_amount="10.00", currency="EUR", status=Order.Status.PENDING,
        )

    def _saleor_orders(self, nodes):
        return {"orders": {"edges": [{"node": n} for n in nodes]}}

    def test_clean_when_all_orders_known(self):
        from io import StringIO

        from django.core.management import call_command
        payload = self._saleor_orders([{"id": "ORD-KNOWN", "userEmail": "alice@example.com",
                                        "total": {"gross": {"amount": 10, "currency": "EUR"}}}])
        out = StringIO()
        with patch("payments.management.commands.reconcile_orders.saleor_graphql",
                   return_value=payload):
            call_command("reconcile_orders", stdout=out)
        self.assertIn("Reconciliation clean", out.getvalue())

    def test_reports_missing_order_and_fixes_with_flag(self):
        from io import StringIO

        from django.core.management import call_command
        payload = self._saleor_orders([
            {"id": "ORD-LOST", "userEmail": "ALICE@example.com",  # case-insensitive match
             "total": {"gross": {"amount": 49.99, "currency": "EUR"}}},
            {"id": "ORD-GHOST", "userEmail": "nobody@example.com",
             "total": {"gross": {"amount": 5, "currency": "EUR"}}},
        ])
        out = StringIO()
        with patch("payments.management.commands.reconcile_orders.saleor_graphql",
                   return_value=payload):
            call_command("reconcile_orders", "--fix", stdout=out)
        output = out.getvalue()
        self.assertIn("MISSING ORD-LOST", output)
        self.assertIn("MISSING ORD-GHOST", output)
        # Matched user recreated as pending; unmatched left for manual review
        lost = Order.objects.get(saleor_order_id="ORD-LOST")
        self.assertEqual(lost.user, self.user)
        self.assertEqual(lost.status, Order.Status.PENDING)
        self.assertFalse(Order.objects.filter(saleor_order_id="ORD-GHOST").exists())

    def test_without_fix_nothing_is_created(self):
        from io import StringIO

        from django.core.management import call_command
        payload = self._saleor_orders([{"id": "ORD-LOST", "userEmail": "alice@example.com",
                                        "total": {"gross": {"amount": 1, "currency": "EUR"}}}])
        with patch("payments.management.commands.reconcile_orders.saleor_graphql",
                   return_value=payload):
            call_command("reconcile_orders", stdout=StringIO())
        self.assertFalse(Order.objects.filter(saleor_order_id="ORD-LOST").exists())


@skipUnless(os.environ.get("SALEOR_INTEGRATION") == "1",
            "Set SALEOR_INTEGRATION=1 with a configured Saleor instance")
class SaleorIntegrationTests(TestCase):
    """End-to-end checks against a real Saleor instance.

    CHECKOUT_ENABLED must stay off until this suite passes:

        SALEOR_INTEGRATION=1 python manage.py test payments \
            --settings=eve.settings.test
    """

    def test_products_expose_purchasable_variants(self):
        from ecommerce.services.saleor_client import fetch_products_from_saleor
        products = fetch_products_from_saleor(first=5)
        self.assertTrue(products, "Saleor returned no products")
        for product in products:
            self.assertTrue((product.get("defaultVariant") or {}).get("id"),
                            f"{product['slug']} has no purchasable variant")

    def test_checkout_created_with_server_side_prices(self):
        from ecommerce.services.saleor_client import fetch_products_from_saleor

        from .services.saleor_checkout import create_checkout
        product = fetch_products_from_saleor(first=1)[0]
        items = [{"variant_id": product["defaultVariant"]["id"], "quantity": 1}]
        checkout = create_checkout("integration@example.com", items)
        self.assertTrue(checkout["checkout_id"])
        self.assertGreater(checkout["total_amount"], 0)
