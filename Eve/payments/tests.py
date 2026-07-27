import hashlib
import hmac
import json
import os
from unittest import skipUnless
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from .models import Order

WEBHOOK_SECRET = "test-webhook-secret"


def _signed(payload: dict, secret: str = WEBHOOK_SECRET):
    body = json.dumps(payload).encode()
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return body, signature


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
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
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


@override_settings(SALEOR_WEBHOOK_SECRET=WEBHOOK_SECRET)
class WebhookTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.order = Order.objects.create(
            user=self.user, saleor_order_id="ORD1",
            total_amount="99.98", currency="EUR", status=Order.Status.PENDING,
        )
        self.url = reverse("saleor_webhook")

    def _post(self, payload, signature=None, secret=WEBHOOK_SECRET):
        body, computed = _signed(payload, secret)
        return self.client.post(
            self.url, data=body, content_type="application/json",
            headers={"Saleor-Signature": signature or computed},
        )

    def test_valid_signature_marks_order_paid(self):
        response = self._post({"event_type": "order_fully_paid",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_invalid_signature_rejected(self):
        response = self._post({"event_type": "order_fully_paid",
                               "order": {"id": "ORD1"}},
                              signature="0" * 64)
        self.assertEqual(response.status_code, 401)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PENDING)

    def test_wrong_secret_rejected(self):
        body, signature = _signed({"event_type": "order_fully_paid",
                                   "order": {"id": "ORD1"}}, secret="attacker")
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            headers={"Saleor-Signature": signature},
        )
        self.assertEqual(response.status_code, 401)

    @override_settings(SALEOR_WEBHOOK_SECRET="")
    def test_missing_secret_rejects_everything(self):
        response = self._post({"event_type": "order_fully_paid",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 401)

    def test_redelivery_is_idempotent(self):
        payload = {"event_type": "order_fully_paid", "order": {"id": "ORD1"}}
        self._post(payload)
        response = self._post(payload)  # same event again
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.PAID)

    def test_invalid_state_transition_refused(self):
        self.order.status = Order.Status.REFUNDED
        self.order.save()
        response = self._post({"event_type": "order_fully_paid",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 409)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REFUNDED)

    def test_refund_flow(self):
        self.order.status = Order.Status.PAID
        self.order.save()
        response = self._post({"event_type": "order_refunded",
                               "order": {"id": "ORD1"}})
        self.assertEqual(response.status_code, 200)
        self.order.refresh_from_db()
        self.assertEqual(self.order.status, Order.Status.REFUNDED)

    def test_unknown_order_acknowledged_without_changes(self):
        response = self._post({"event_type": "order_fully_paid",
                               "order": {"id": "GHOST"}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"handled": False})

    def test_malformed_payload_rejected(self):
        body = b"not json"
        signature = hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
        response = self.client.post(
            self.url, data=body, content_type="application/json",
            headers={"Saleor-Signature": signature},
        )
        self.assertEqual(response.status_code, 400)


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


@override_settings(CHECKOUT_ENABLED=True, SALEOR_WEBHOOK_SECRET=WEBHOOK_SECRET)
class EndToEndOrderJourneyTests(TestCase):
    """Full journey with Saleor and Mongo mocked at the service boundary:
    register -> add to cart -> checkout -> webhook payment -> paid history,
    plus the refund and duplicate-delivery paths."""

    def setUp(self):
        cache.clear()

    def _register_and_login(self):
        self.client.post(reverse("register"), {
            "username": "carol", "email": "carol@example.com",
            "password1": "S3curePass!x", "password2": "S3curePass!x",
        })  # register logs the user in

    def test_full_purchase_refund_and_redelivery(self):
        self._register_and_login()
        user = User.objects.get(username="carol")

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
        payload = {"event_type": "order_fully_paid", "order": {"id": "ORD-E2E"}}
        body, signature = _signed(payload)
        for _ in range(2):
            response = self.client.post(
                reverse("saleor_webhook"), data=body,
                content_type="application/json",
                headers={"Saleor-Signature": signature},
            )
            self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.PAID)
        self.assertEqual(Order.objects.count(), 1)

        # History shows the paid order
        response = self.client.get(reverse("payment_history"))
        self.assertContains(response, "ORD-E2E")
        self.assertContains(response, "Paid")

        # Refund webhook completes the lifecycle
        body, signature = _signed(
            {"event_type": "order_refunded", "order": {"id": "ORD-E2E"}})
        response = self.client.post(
            reverse("saleor_webhook"), data=body,
            content_type="application/json",
            headers={"Saleor-Signature": signature},
        )
        self.assertEqual(response.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, Order.Status.REFUNDED)


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
