"""v1 API tests: contract, authentication, authorization, and error shape."""
from unittest.mock import patch

from accounts.models import Profile
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from payments.models import Order
from payments.services.saleor_checkout import CheckoutError
from rest_framework.authtoken.models import Token


def make_product(**overrides):
    product = {
        "id": "UHJvZHVjdDox",
        "name": "Eve Horizon",
        "slug": "eve-horizon",
        "description": "A calm VR walk.",
        "thumbnail": {"url": "https://cdn.example.com/x.png"},
        "defaultVariant": {"id": "V1"},
        "pricing": {
            "priceRange": {
                "start": {"gross": {"amount": 49.99, "currency": "EUR"}},
                "stop": {"gross": {"amount": 49.99, "currency": "EUR"}},
            }
        },
    }
    product.update(overrides)
    return product


def make_cart(user_id, items=None):
    return {
        "user_id": user_id,
        "items": items
        if items is not None
        else [
            {
                "product_id": "UHJvZHVjdDox",
                "slug": "eve-horizon",
                "name": "Eve Horizon",
                "variant_id": "V1",
                "price_amount": 49.99,
                "price_currency": "EUR",
                "quantity": 2,
                "thumbnail_url": "https://cdn.example.com/x.png",
            }
        ],
        "updated_at": None,
    }


class ApiTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user("alice", "alice@example.com", "S3curePass!x")
        Profile.objects.create(user=self.user, email_verified=True)


class ProductEndpointTests(ApiTestCase):
    def test_catalogue_is_public_and_flattens_upstream_shape(self):
        with patch("api.v1.views.list_products", return_value=([make_product()], False)):
            response = self.client.get("/api/v1/products/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        product = body["results"][0]
        # Stable client contract, not Saleor's nested GraphQL shape
        self.assertEqual(product["slug"], "eve-horizon")
        self.assertEqual(product["price"], {"amount": 49.99, "currency": "EUR"})
        self.assertTrue(product["purchasable"])
        self.assertNotIn("pricing", product)

    def test_degraded_flag_marks_stale_data(self):
        with patch("api.v1.views.list_products", return_value=([make_product()], True)):
            response = self.client.get("/api/v1/products/")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["degraded"])

    def test_empty_catalogue_outage_returns_503_not_fake_products(self):
        with patch("api.v1.views.list_products", return_value=([], True)):
            response = self.client.get("/api/v1/products/")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "catalogue_unavailable")

    def test_detail_returns_404_envelope_for_unknown_slug(self):
        from ecommerce.services.catalogue import ProductNotFound

        with patch("api.v1.views.get_product", side_effect=ProductNotFound("ghost")):
            response = self.client.get("/api/v1/products/ghost/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "product_not_found")


class AuthenticationTests(ApiTestCase):
    def test_cart_requires_authentication(self):
        response = self.client.get("/api/v1/cart/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_required")
        # Tells a client how to authenticate, per RFC 7235
        self.assertIn("Token", response["WWW-Authenticate"])

    def test_token_auth_works_for_non_browser_clients(self):
        token = Token.objects.create(user=self.user)
        with patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)):
            response = self.client.get(
                "/api/v1/cart/", HTTP_AUTHORIZATION=f"Token {token.key}"
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["item_count"], 2)

    def test_invalid_token_rejected(self):
        response = self.client.get("/api/v1/cart/", HTTP_AUTHORIZATION="Token deadbeef")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "authentication_failed")

    def test_token_endpoint_issues_token_for_valid_credentials(self):
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "alice", "password": "S3curePass!x"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["token"])

    def test_token_endpoint_rejects_bad_credentials(self):
        response = self.client.post(
            "/api/v1/auth/token/", {"username": "alice", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "validation_error")


class CartEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def test_get_cart_computes_totals(self):
        with patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)):
            response = self.client.get("/api/v1/cart/")
        body = response.json()
        self.assertEqual(body["item_count"], 2)
        self.assertEqual(body["estimated_total"], {"amount": 99.98, "currency": "EUR"})

    def test_add_item_validates_and_delegates_to_the_service(self):
        with (
            patch("api.v1.views.get_product", return_value=make_product()),
            patch("api.v1.views.cart_service.add_to_cart") as add,
            patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)),
        ):
            response = self.client.post(
                "/api/v1/cart/items/",
                {"slug": "eve-horizon", "quantity": 2},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 201)
        add.assert_called_once()
        self.assertEqual(add.call_args.args[0], self.user.id)  # never client-supplied

    def test_add_item_rejects_out_of_range_quantity(self):
        response = self.client.post(
            "/api/v1/cart/items/",
            {"slug": "eve-horizon", "quantity": 999},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        body = response.json()["error"]
        self.assertEqual(body["code"], "validation_error")
        self.assertIn("quantity", body["details"])

    def test_update_quantity_sets_absolute_value(self):
        with (
            patch("api.v1.views.cart_service.set_item_quantity", return_value=True) as setter,
            patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)),
        ):
            response = self.client.patch(
                "/api/v1/cart/items/UHJvZHVjdDox/",
                {"quantity": 5},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 200)
        setter.assert_called_once_with(self.user.id, "UHJvZHVjdDox", 5)

    def test_update_missing_item_returns_404_envelope(self):
        with patch("api.v1.views.cart_service.set_item_quantity", return_value=False):
            response = self.client.patch(
                "/api/v1/cart/items/nope/",
                {"quantity": 5},
                content_type="application/json",
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "cart_item_not_found")

    def test_delete_item_and_clear_cart(self):
        with patch("api.v1.views.cart_service.remove_from_cart") as remove:
            response = self.client.delete("/api/v1/cart/items/UHJvZHVjdDox/")
        self.assertEqual(response.status_code, 204)
        remove.assert_called_once_with(self.user.id, "UHJvZHVjdDox")

        with patch("api.v1.views.cart_service.clear_cart") as clear:
            response = self.client.delete("/api/v1/cart/")
        self.assertEqual(response.status_code, 204)
        clear.assert_called_once_with(self.user.id)


@override_settings(CHECKOUT_ENABLED=True)
class CheckoutEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.user)

    def _order(self, key="key-1"):
        return Order.objects.create(
            user=self.user, saleor_order_id="ORD-API-1", idempotency_key=key,
            total_amount="99.98", currency="EUR", status=Order.Status.PENDING,
        )

    def test_places_order_through_the_shared_service(self):
        # The order must not exist before the call, or the view would
        # (correctly) treat the request as an idempotent replay
        def place_order(**kwargs):
            return self._order(key=kwargs["idempotency_key"])

        with (
            patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)),
            patch("api.v1.views.cart_service.clear_cart") as clear,
            patch("api.v1.views.place_order_once", side_effect=place_order) as place,
        ):
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "key-1"}
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["saleor_order_id"], "ORD-API-1")
        self.assertEqual(place.call_args.kwargs["idempotency_key"], "key-1")
        clear.assert_called_once()

    def test_missing_idempotency_key_is_rejected(self):
        with patch("api.v1.views.place_order_once") as place:
            response = self.client.post("/api/v1/checkout/")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "idempotency_key_required")
        place.assert_not_called()

    def test_replayed_key_returns_the_original_order_without_charging_again(self):
        order = self._order(key="key-replay")
        with patch("api.v1.views.place_order_once") as place:
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "key-replay"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["id"], order.id)
        place.assert_not_called()  # no second Saleor mutation
        self.assertEqual(Order.objects.count(), 1)

    def test_concurrent_attempt_returns_409_and_tells_client_not_to_retry(self):
        with (
            patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)),
            patch("api.v1.views.place_order_once", return_value=None),
        ):
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "key-busy"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "checkout_in_progress")

    def test_checkout_error_is_surfaced_as_a_conflict(self):
        with (
            patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)),
            patch("api.v1.views.place_order_once", side_effect=CheckoutError("Your cart is empty.")),
        ):
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "key-empty"}
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["message"], "Your cart is empty.")

    def test_unverified_email_cannot_checkout(self):
        Profile.objects.filter(user=self.user).update(email_verified=False)
        with patch("api.v1.views.place_order_once") as place:
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "key-2"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "email_not_verified")
        place.assert_not_called()

    @override_settings(CHECKOUT_ENABLED=False)
    def test_disabled_checkout_returns_503(self):
        response = self.client.post("/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "k"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "checkout_disabled")


class OrderEndpointTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.bob = User.objects.create_user("bob", "bob@example.com", "S3curePass!x")
        self.mine = Order.objects.create(
            user=self.user, saleor_order_id="ORD-MINE", idempotency_key="k1",
            total_amount="10.00", currency="EUR", status=Order.Status.PAID,
        )
        Order.objects.create(
            user=self.bob, saleor_order_id="ORD-BOBS", idempotency_key="k2",
            total_amount="20.00", currency="EUR", status=Order.Status.PAID,
        )
        self.client.force_login(self.user)

    def test_list_is_paginated_and_scoped_to_the_user(self):
        response = self.client.get("/api/v1/orders/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["results"][0]["saleor_order_id"], "ORD-MINE")
        self.assertNotContains(response, "ORD-BOBS")

    def test_cannot_retrieve_another_users_order(self):
        response = self.client.get("/api/v1/orders/ORD-BOBS/")
        self.assertEqual(response.status_code, 404)

    def test_retrieve_own_order_exposes_the_money_contract(self):
        response = self.client.get(f"/api/v1/orders/{self.mine.saleor_order_id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total"], {"amount": "10.00", "currency": "EUR"})

    def test_orders_are_read_only(self):
        response = self.client.post("/api/v1/orders/", {})
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.json()["error"]["code"], "method_not_allowed")


class ProfileEndpointTests(ApiTestCase):
    def test_profile_never_exposes_health_adjacent_fields(self):
        Profile.objects.filter(user=self.user).update(
            hospital_name="St. Mary", room_number="12B"
        )
        self.client.force_login(self.user)
        response = self.client.get("/api/v1/profile/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["user"]["username"], "alice")
        self.assertNotIn("hospital_name", body)
        self.assertNotIn("room_number", body)


class ErrorContractTests(ApiTestCase):
    def test_every_error_carries_code_message_details_and_request_id(self):
        response = self.client.get("/api/v1/cart/")
        error = response.json()["error"]
        self.assertEqual(
            set(error), {"code", "message", "details", "request_id"}
        )
        self.assertTrue(error["request_id"], "errors must be traceable to logs")

    def test_unknown_route_returns_the_envelope_not_html(self):
        response = self.client.get("/api/v1/does-not-exist/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response["Content-Type"], "application/json")
        self.assertEqual(response.json()["error"]["code"], "not_found")


class OpenAPISchemaTests(TestCase):
    """The published contract must stay accurate and machine-consumable."""

    def test_schema_is_served_and_covers_every_endpoint(self):
        response = self.client.get("/api/v1/schema/?format=json")
        self.assertEqual(response.status_code, 200)
        schema = response.json()
        self.assertEqual(schema["openapi"][:3], "3.0")
        for path in (
            "/api/v1/products/", "/api/v1/products/{slug}/", "/api/v1/cart/",
            "/api/v1/cart/items/", "/api/v1/cart/items/{product_id}/",
            "/api/v1/checkout/", "/api/v1/orders/", "/api/v1/orders/{saleor_order_id}/",
            "/api/v1/profile/", "/api/v1/auth/token/",
        ):
            self.assertIn(path, schema["paths"], f"{path} missing from the schema")

    def test_schema_generates_without_warnings_or_errors(self):
        # A schema full of "unable to guess serializer" is worse than none
        from drf_spectacular.drainage import GENERATOR_STATS
        from drf_spectacular.generators import SchemaGenerator

        GENERATOR_STATS.reset()
        SchemaGenerator().get_schema(request=None, public=True)
        self.assertEqual(len(GENERATOR_STATS._warn_cache), 0, GENERATOR_STATS._warn_cache)
        self.assertEqual(len(GENERATOR_STATS._error_cache), 0, GENERATOR_STATS._error_cache)

    def test_legacy_unversioned_route_is_not_published(self):
        schema = self.client.get("/api/v1/schema/?format=json").json()
        self.assertNotIn("/api/profile/", schema["paths"])

    def test_checkout_documents_the_idempotency_key_header(self):
        schema = self.client.get("/api/v1/schema/?format=json").json()
        params = schema["paths"]["/api/v1/checkout/"]["post"]["parameters"]
        header = next(p for p in params if p["name"] == "Idempotency-Key")
        self.assertEqual(header["in"], "header")
        self.assertTrue(header["required"])

    def test_error_envelope_is_part_of_the_published_contract(self):
        schema = self.client.get("/api/v1/schema/?format=json").json()
        error = schema["components"]["schemas"]["Error"]["properties"]["error"]
        self.assertTrue(error)
        responses = schema["paths"]["/api/v1/cart/"]["get"]["responses"]
        self.assertIn("401", responses)

    def test_swagger_ui_renders_with_local_assets_under_the_csp(self):
        response = self.client.get("/api/v1/docs/")
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Assets come from our own /static/, never a CDN
        self.assertNotIn("unpkg.com", body)
        self.assertNotIn("cdn.jsdelivr.net", body)
        csp = response["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)  # scripts stay locked down
        self.assertIn("style-src 'self' 'unsafe-inline'", csp)  # UI needs inline styles

    def test_docs_have_no_inline_script_so_the_strict_csp_cannot_blank_them(self):
        # Regression: the default Swagger view inlines its bootstrap script,
        # which script-src 'self' blocks — the page returned 200 but rendered
        # nothing. The split view serves that script as its own request.
        import re

        response = self.client.get("/api/v1/docs/")
        inline = [
            body for body in re.findall(
                r"<script(?![^>]*src=)[^>]*>(.*?)</script>",
                response.content.decode(), re.S,
            ) if body.strip()
        ]
        self.assertEqual(inline, [], "inline scripts are blocked by the CSP")
        csp = response["Content-Security-Policy"]
        self.assertIn("script-src 'self'", csp)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", csp)

    def test_swagger_init_script_is_served_as_its_own_request(self):
        response = self.client.get("/api/v1/docs/?script=")
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])

    def test_redoc_is_served(self):
        self.assertEqual(self.client.get("/api/v1/redoc/").status_code, 200)

    def test_site_wide_csp_is_not_weakened_by_the_docs_exception(self):
        response = self.client.get("/api/v1/products/")
        self.assertIn("style-src 'self'", response["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response["Content-Security-Policy"])
