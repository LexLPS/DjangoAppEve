from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .services.saleor_client import SaleorAPIError


def make_product(**overrides):
    product = {
        "id": "UHJvZHVjdDox",
        "name": "Eve Horizon",
        "slug": "eve-horizon",
        "description": "A calm VR walk.",
        "thumbnail": {"url": "https://cdn.example.com/x.png"},
        "pricing": {
            "priceRange": {
                "start": {"gross": {"amount": 49.99, "currency": "EUR"}},
                "stop": {"gross": {"amount": 49.99, "currency": "EUR"}},
            }
        },
    }
    product.update(overrides)
    return product


class XssTests(TestCase):
    """Product data comes from external systems and must never reach the
    page as executable markup."""

    def test_detail_page_escapes_malicious_product_fields(self):
        evil = make_product(
            name='<script>alert("name")</script>Evil',
            description='<img src=x onerror=alert(1)><script>steal()</script>Hi',
        )
        with patch("ecommerce.views.get_cached_product", return_value=evil):
            response = self.client.get(
                reverse("product_detail", args=["eve-horizon"])
            )
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertNotIn("<script>", html)
        self.assertNotIn("onerror", html)

    def test_catalogue_escapes_malicious_product_fields(self):
        evil = make_product(name='<script>alert(1)</script>')
        with patch("ecommerce.views.get_cached_products", return_value=[evil]):
            response = self.client.get(reverse("product_catalogue"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("<script>alert", response.content.decode())


class UpstreamErrorTests(TestCase):
    def test_saleor_failure_shows_generic_message_only(self):
        error = SaleorAPIError(
            "Saleor endpoint did not return JSON (content-type=text/html). "
            "Body starts with: '<html>internal secret'"
        )
        with patch("ecommerce.views.get_cached_products", return_value=[]), \
             patch("ecommerce.views.fetch_products_from_saleor", side_effect=error), \
             patch("ecommerce.views.cache_product"):
            response = self.client.get(reverse("product_catalogue"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("temporarily unavailable", html)
        self.assertNotIn("internal secret", html)
        self.assertNotIn("content-type", html)

    def test_invalid_external_products_are_dropped(self):
        bad = [{"id": None}, {"name": "no id or slug"}, "not-a-dict"]
        with patch("ecommerce.views.get_cached_products", return_value=[]), \
             patch("ecommerce.views.fetch_products_from_saleor", return_value=bad), \
             patch("ecommerce.views.cache_product") as cache_mock:
            response = self.client.get(reverse("product_catalogue"))
        self.assertEqual(response.status_code, 200)
        cache_mock.assert_not_called()


class CartInputValidationTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.client.force_login(self.user)
        self.url = reverse("add_to_cart", args=["eve-horizon"])

    def post_quantity(self, quantity):
        with patch("ecommerce.views.get_cached_product", return_value=make_product()), \
             patch("ecommerce.views.add_to_cart") as add:
            response = self.client.post(self.url, {"quantity": quantity})
        return response, add

    def test_rejects_non_numeric_quantity(self):
        response, add = self.post_quantity("abc")
        self.assertEqual(response.status_code, 400)
        add.assert_not_called()

    def test_rejects_zero_and_negative_quantity(self):
        for bad in ("0", "-3"):
            response, add = self.post_quantity(bad)
            self.assertEqual(response.status_code, 400)
            add.assert_not_called()

    def test_rejects_excessive_quantity(self):
        response, add = self.post_quantity("21")
        self.assertEqual(response.status_code, 400)
        add.assert_not_called()

    def test_accepts_valid_quantity_for_current_user_only(self):
        with patch("ecommerce.views.get_cached_product", return_value=make_product()), \
             patch("ecommerce.views.add_to_cart") as add:
            response = self.client.post(self.url, {"quantity": "3"})
        self.assertRedirects(response, reverse("cart"), fetch_redirect_response=False)
        add.assert_called_once()
        called_user_id = add.call_args.args[0]
        self.assertEqual(called_user_id, self.user.id)  # cart keyed to session user

    def test_add_to_cart_requires_post(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 405)

    def test_remove_uses_session_user_id(self):
        with patch("ecommerce.views.remove_from_cart") as remove:
            response = self.client.post(reverse("remove_from_cart", args=["prod-1"]))
        self.assertRedirects(response, reverse("cart"), fetch_redirect_response=False)
        remove.assert_called_once_with(self.user.id, "prod-1")


class AtomicCartServiceTests(TestCase):
    """Cart mutations must be single atomic Mongo operators, never
    read-modify-write cycles that can lose concurrent updates."""

    def _run_add(self, matched_existing: bool):
        with patch("ecommerce.services.cart_service.carts_collection") as coll:
            coll.update_one.return_value = MagicMock(
                matched_count=1 if matched_existing else 0
            )
            from .services.cart_service import add_to_cart
            add_to_cart(7, make_product(), quantity=2)
        return coll

    def test_existing_item_uses_atomic_increment(self):
        coll = self._run_add(matched_existing=True)
        first_update = coll.update_one.call_args_list[0]
        self.assertIn("$inc", first_update.args[1])
        # No $push happened — only the $inc and the quantity clamp
        operators = [list(call.args[1].keys()) for call in coll.update_one.call_args_list]
        self.assertFalse(any("$push" in ops for ops in operators))

    def test_new_item_uses_guarded_push(self):
        coll = self._run_add(matched_existing=False)
        push_call = coll.update_one.call_args_list[1]
        self.assertIn("$push", push_call.args[1])
        # The filter guards against a concurrent push of the same product
        self.assertEqual(push_call.args[0]["items.product_id"], {"$ne": "UHJvZHVjdDox"})
        pushed = push_call.args[1]["$push"]["items"]
        self.assertEqual(pushed["variant_id"], None)  # no defaultVariant in fixture

    def test_remove_uses_atomic_pull(self):
        with patch("ecommerce.services.cart_service.carts_collection") as coll:
            from .services.cart_service import remove_from_cart
            remove_from_cart(7, "P1")
        update = coll.update_one.call_args
        self.assertIn("$pull", update.args[1])
        self.assertEqual(update.args[1]["$pull"]["items"], {"product_id": "P1"})


class CartRobustnessTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        self.client.force_login(self.user)

    def test_cart_view_tolerates_malformed_stored_items(self):
        malformed_cart = {
            "user_id": self.user.id,
            "items": [
                {"product_id": "1", "name": "ok", "price_amount": 10.0,
                 "price_currency": "EUR", "quantity": 2},
                {"product_id": "2", "name": "no price"},
                {"product_id": "3", "name": "bad types", "price_amount": "x",
                 "quantity": "y"},
            ],
        }
        with patch("ecommerce.views.get_cart", return_value=malformed_cart):
            response = self.client.get(reverse("cart"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_amount"], 20.0)
