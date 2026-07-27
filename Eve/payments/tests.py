from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .models import Order


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

    def test_authenticated_post_is_disabled_and_creates_no_order(self):
        self.client.force_login(self.user)
        response = self.client.post(reverse("checkout"))
        self.assertEqual(response.status_code, 405)
        self.assertEqual(Order.objects.count(), 0)

    def test_history_requires_login(self):
        response = self.client.get(reverse("payment_history"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("login"), response.url)


class OrderOwnershipTests(TestCase):
    def setUp(self):
        cache.clear()
        self.alice = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.bob = User.objects.create_user("bob", "bob@example.com", "S3curePass!x")
        Order.objects.create(
            user=self.alice, saleor_order_id="SO_ALICE_1",
            total_amount="10.00", currency="EUR", status="created",
        )
        Order.objects.create(
            user=self.bob, saleor_order_id="SO_BOB_1",
            total_amount="20.00", currency="EUR", status="created",
        )

    def test_history_shows_only_own_orders(self):
        self.client.force_login(self.alice)
        response = self.client.get(reverse("payment_history"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "SO_ALICE_1")
        self.assertNotContains(response, "SO_BOB_1")
