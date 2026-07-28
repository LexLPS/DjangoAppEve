from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .services import saleor_client
from .services.saleor_client import SaleorAPIError, SaleorCircuitOpen


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
        error = SaleorAPIError("non_json_response", status=502, content_type="text/html")
        with patch("ecommerce.views.get_cached_products", return_value=[]), \
             patch("ecommerce.views.fetch_products_from_saleor", side_effect=error), \
             patch("ecommerce.views.cache_product"):
            response = self.client.get(reverse("product_catalogue"))
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("temporarily unavailable", html)
        self.assertNotIn("saleor_error", html)
        self.assertNotIn("content_type", html)

    def test_invalid_external_products_are_dropped(self):
        bad = [{"id": None}, {"name": "no id or slug"}, "not-a-dict"]
        with patch("ecommerce.views.get_cached_products", return_value=[]), \
             patch("ecommerce.views.fetch_products_from_saleor", return_value=bad), \
             patch("ecommerce.views.cache_product") as cache_mock:
            response = self.client.get(reverse("product_catalogue"))
        self.assertEqual(response.status_code, 200)
        cache_mock.assert_not_called()


class NegativeCacheTests(TestCase):
    """R3: repeated requests for unknown slugs must not repeatedly hit
    Saleor."""

    def setUp(self):
        cache.clear()

    def test_unknown_slug_hits_saleor_only_once(self):
        with patch("ecommerce.views.get_cached_product", return_value=None), \
             patch("ecommerce.views.fetch_product_by_slug",
                   return_value=None) as fetch:
            first = self.client.get(reverse("product_detail", args=["ghost"]))
            second = self.client.get(reverse("product_detail", args=["ghost"]))
        self.assertEqual(first.status_code, 404)
        self.assertEqual(second.status_code, 404)
        fetch.assert_called_once()  # second request served from negative cache

    def test_negative_cache_does_not_block_other_slugs(self):
        with patch("ecommerce.views.get_cached_product", return_value=None), \
             patch("ecommerce.views.fetch_product_by_slug",
                   return_value=None) as fetch:
            self.client.get(reverse("product_detail", args=["ghost-a"]))
            self.client.get(reverse("product_detail", args=["ghost-b"]))
        self.assertEqual(fetch.call_count, 2)

    def test_valid_product_not_negatively_cached(self):
        with patch("ecommerce.views.get_cached_product", return_value=None), \
             patch("ecommerce.views.fetch_product_by_slug",
                   return_value=make_product()), \
             patch("ecommerce.views.cache_product"):
            response = self.client.get(reverse("product_detail", args=["eve-horizon"]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(cache.get("product-miss:eve-horizon"))


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


def _mock_response(status=200, content_type="application/json",
                   json_data=None, json_error=False, body=b""):
    response = MagicMock()
    response.status_code = status
    response.headers = {"content-type": content_type}
    response.content = body
    if json_error:
        response.json.side_effect = ValueError("bad json")
    else:
        response.json.return_value = json_data if json_data is not None else {}
    return response


@patch.object(saleor_client, "SALEOR_GRAPHQL_URL", "https://saleor.example.com/graphql/")
@patch.object(saleor_client, "_backoff_sleep", lambda attempt: None)
class SaleorClientResilienceTests(TestCase):
    """Connection errors, timeouts, bad payloads, retries, circuit breaker,
    and log hygiene (no response bodies in error text)."""

    def setUp(self):
        cache.clear()  # closed circuit at the start of every test

    def test_connection_error_retries_then_fails(self):
        with patch.object(saleor_client._session, "post",
                          side_effect=requests.ConnectionError("boom")) as post:
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.saleor_graphql("query {}", {})
        self.assertEqual(post.call_count, 3)  # bounded retries
        self.assertIn("ConnectionError", str(ctx.exception))

    def test_timeout_raises_clean_error(self):
        with patch.object(saleor_client._session, "post",
                          side_effect=requests.Timeout("slow")):
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.saleor_graphql("query {}", {})
        self.assertEqual(ctx.exception.code, "timeout")

    def test_http_error_status_reported_without_body(self):
        response = _mock_response(status=500, body=b"<html>stack trace secret</html>")
        with patch.object(saleor_client._session, "post", return_value=response):
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.saleor_graphql("query {}", {}, retry=False)
        self.assertEqual(ctx.exception.code, "http_error")
        self.assertEqual(ctx.exception.status, 500)
        self.assertIn("status=500", str(ctx.exception))
        self.assertNotIn("secret", str(ctx.exception))

    def test_non_json_content_type_reported_without_body(self):
        response = _mock_response(content_type="text/html",
                                  body=b"<html>login page secret</html>")
        with patch.object(saleor_client._session, "post", return_value=response):
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.saleor_graphql("query {}", {}, retry=False)
        # Structured metadata only: code, status, content type — never bodies
        self.assertEqual(ctx.exception.code, "non_json_response")
        self.assertEqual(ctx.exception.content_type, "text/html")
        self.assertNotIn("secret", str(ctx.exception))

    def test_invalid_json_rejected(self):
        response = _mock_response(json_error=True)
        with patch.object(saleor_client._session, "post", return_value=response):
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.saleor_graphql("query {}", {}, retry=False)
        self.assertEqual(ctx.exception.code, "invalid_json")

    def test_graphql_errors_report_codes_not_messages(self):
        response = _mock_response(json_data={"errors": [
            {"message": "user email leaked@example.com",
             "extensions": {"code": "GRAPHQL_ERROR"}},
        ]})
        with patch.object(saleor_client._session, "post", return_value=response):
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.saleor_graphql("query {}", {}, retry=False)
        self.assertIn("GRAPHQL_ERROR", str(ctx.exception))
        self.assertNotIn("leaked@example.com", str(ctx.exception))

    def test_incomplete_products_response_rejected(self):
        response = _mock_response(json_data={"data": {"products": None}})
        with patch.object(saleor_client._session, "post", return_value=response):
            with self.assertRaises(SaleorAPIError) as ctx:
                saleor_client.fetch_products_from_saleor()
        self.assertIn("incomplete", str(ctx.exception))

    def test_retryable_status_retries_reads_only(self):
        response = _mock_response(status=503)
        with patch.object(saleor_client._session, "post",
                          return_value=response) as post:
            with self.assertRaises(SaleorAPIError):
                saleor_client.saleor_graphql("query {}", {})  # read: retries
        self.assertEqual(post.call_count, 3)

        post.reset_mock()
        with patch.object(saleor_client._session, "post",
                          return_value=response) as post:
            with self.assertRaises(SaleorAPIError):
                saleor_client.saleor_graphql("mutation {}", {}, retry=False)
        self.assertEqual(post.call_count, 1)  # mutations never retry

    def test_circuit_opens_after_consecutive_failures_and_fails_fast(self):
        response = _mock_response(status=500)
        with patch.object(saleor_client._session, "post",
                          return_value=response):
            for _ in range(saleor_client.CIRCUIT_FAILURE_THRESHOLD):
                with self.assertRaises(SaleorAPIError):
                    saleor_client.saleor_graphql("query {}", {}, retry=False)

        # Circuit is now open: no HTTP call happens at all
        with patch.object(saleor_client._session, "post") as post:
            with self.assertRaises(SaleorCircuitOpen):
                saleor_client.saleor_graphql("query {}", {}, retry=False)
        post.assert_not_called()

    def test_success_resets_failure_streak(self):
        good = _mock_response(json_data={"data": {"ok": True}})
        bad = _mock_response(status=500)
        with patch.object(saleor_client._session, "post",
                          side_effect=[bad, bad, good, bad]):
            for _ in range(2):
                with self.assertRaises(SaleorAPIError):
                    saleor_client.saleor_graphql("query {}", {}, retry=False)
            saleor_client.saleor_graphql("query {}", {}, retry=False)  # success
            with self.assertRaises(SaleorAPIError):
                saleor_client.saleor_graphql("query {}", {}, retry=False)
        # Streak was reset by the success — circuit must still be closed
        self.assertFalse(saleor_client._circuit_is_open())


class ExternalUrlSanitizationTests(TestCase):
    def test_javascript_thumbnail_urls_never_rendered(self):
        evil = make_product(thumbnail={"url": "javascript:alert(1)"})
        with patch("ecommerce.views.get_cached_products", return_value=[evil]):
            response = self.client.get(reverse("product_catalogue"))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("javascript:", response.content.decode())

    def test_data_and_malformed_media_urls_dropped_on_detail(self):
        evil = make_product(
            thumbnail={"url": "data:text/html,<script>x</script>"},
            media=[{"url": "javascript:alert(1)"}, {"url": "https://cdn.example.com/ok.png"}],
        )
        with patch("ecommerce.views.get_cached_product", return_value=evil):
            response = self.client.get(reverse("product_detail", args=["eve-horizon"]))
        html = response.content.decode()
        self.assertNotIn("javascript:", html)
        self.assertNotIn("data:text/html", html)
        self.assertIn("https://cdn.example.com/ok.png", html)


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
