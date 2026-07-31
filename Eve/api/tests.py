"""v1 API tests: contract, authentication, authorization, and error shape."""
from unittest.mock import patch

from accounts.models import Profile
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from payments.models import Order
from payments.services.saleor_checkout import CheckoutError

from api.models import RefreshToken
from api.tokens import hash_refresh_token, issue_access_token


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
        token_key, _ = issue_access_token(self.user)
        with patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)):
            response = self.client.get(
                "/api/v1/cart/", HTTP_AUTHORIZATION=f"Token {token_key}"
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
        # Issuance now returns a short-lived access token plus a refresh
        body = response.json()
        self.assertTrue(body["access"])
        self.assertTrue(body["refresh"])

    def test_token_endpoint_rejects_bad_credentials(self):
        response = self.client.post(
            "/api/v1/auth/token/", {"username": "alice", "password": "wrong"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_credentials")


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
        from payments.services.checkout import scoped_idempotency_key

        # Stored scoped to the user, exactly as place_order_once writes it
        return Order.objects.create(
            user=self.user, saleor_order_id="ORD-API-1",
            idempotency_key=scoped_idempotency_key(self.user, key),
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
        # This assertion covers response security headers, not catalogue I/O.
        # Keep it independent of a live MongoDB service in CI.
        with patch("api.v1.views.list_products", return_value=([], False)):
            response = self.client.get("/api/v1/products/")
        self.assertIn("style-src 'self'", response["Content-Security-Policy"])
        self.assertNotIn("unsafe-inline", response["Content-Security-Policy"])


@override_settings(CHECKOUT_ENABLED=True)
class IdempotencyKeyScopingTests(ApiTestCase):
    """API clients choose their own Idempotency-Key. Two accounts picking the
    same string must never see each other's orders or block each other."""

    def setUp(self):
        super().setUp()
        self.mallory = User.objects.create_user(
            "mallory", "mallory@example.com", "S3curePass!x"
        )
        Profile.objects.create(user=self.mallory, email_verified=True)

    def _alice_places_order(self, key="shared-key-1"):
        from payments.services.checkout import scoped_idempotency_key

        return Order.objects.create(
            user=self.user, saleor_order_id="ORD-ALICE",
            idempotency_key=scoped_idempotency_key(self.user, key),
            total_amount="99.98", currency="EUR", status=Order.Status.PENDING,
        )

    def test_reused_key_does_not_disclose_another_users_order(self):
        alice_order = self._alice_places_order()
        self.client.force_login(self.mallory)

        with (
            patch("api.v1.views.cart_service.get_cart",
                  return_value=make_cart(self.mallory.id)),
            patch("api.v1.views.cart_service.clear_cart"),
            patch("api.v1.views.place_order_once", return_value=None) as place,
        ):
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "shared-key-1"}
            )
        # Mallory must not receive Alice's order under any status code
        body = response.content.decode()
        self.assertNotIn("ORD-ALICE", body)
        self.assertNotIn(str(alice_order.id), body.split("request_id")[0])
        place.assert_called_once()  # not short-circuited by Alice's record

    def test_same_key_still_replays_for_its_owner(self):
        alice_order = self._alice_places_order(key="replay-key")
        self.client.force_login(self.user)
        with patch("api.v1.views.place_order_once") as place:
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "replay-key"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["saleor_order_id"], alice_order.saleor_order_id)
        place.assert_not_called()

    def test_one_account_cannot_block_anothers_checkout_with_a_guessed_key(self):
        from payments.models import CheckoutAttempt
        from payments.services.checkout import scoped_idempotency_key

        # Mallory pre-registers an in-flight attempt under a guessable key
        CheckoutAttempt.objects.create(
            user=self.mallory,
            idempotency_key=scoped_idempotency_key(self.mallory, "guessable"),
            cart_fingerprint="x",
            state=CheckoutAttempt.State.COMPLETING,
        )
        # Alice uses the same string and must be unaffected
        self.client.force_login(self.user)
        with (
            patch("api.v1.views.cart_service.get_cart", return_value=make_cart(self.user.id)),
            patch("api.v1.views.cart_service.clear_cart"),
            patch("api.v1.views.place_order_once") as place,
        ):
            # Created when the view calls the service, not before it
            place.side_effect = lambda **kwargs: Order.objects.create(
                user=self.user, saleor_order_id="ORD-ALICE-OK",
                idempotency_key=scoped_idempotency_key(self.user, "guessable"),
                total_amount="10.00", currency="EUR", status=Order.Status.PENDING,
            )
            response = self.client.post(
                "/api/v1/checkout/", **{"HTTP_IDEMPOTENCY_KEY": "guessable"}
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json()["saleor_order_id"], "ORD-ALICE-OK")

    def test_scoping_is_per_user_and_deterministic(self):
        from payments.services.checkout import scoped_idempotency_key

        self.assertEqual(
            scoped_idempotency_key(self.user, "k"), scoped_idempotency_key(self.user, "k")
        )
        self.assertNotEqual(
            scoped_idempotency_key(self.user, "k"),
            scoped_idempotency_key(self.mallory, "k"),
        )


class ServerTimingExposureTests(ApiTestCase):
    """R13: the timing side channel must be off in production."""

    @override_settings(SERVER_TIMING_ENABLED=True)
    def test_header_present_when_enabled(self):
        response = self.client.get("/api/v1/products/", **{"HTTP_ACCEPT": "application/json"})
        self.assertIn("Server-Timing", response.headers)

    @override_settings(SERVER_TIMING_ENABLED=False)
    def test_header_absent_when_disabled(self):
        response = self.client.get("/api/v1/products/", **{"HTTP_ACCEPT": "application/json"})
        self.assertNotIn("Server-Timing", response.headers)

    def test_production_settings_disable_it_by_default(self):
        # Asserted at source level: importing prod.py raises without a
        # complete production environment, and what matters here is the
        # default that ships, not the toggle's mechanics.
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent / "eve" / "settings" / "prod.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'SERVER_TIMING_ENABLED = config("SERVER_TIMING_ENABLED", default=False',
            source,
        )


class ThrottleFailOpenTests(ApiTestCase):
    """DRF's stock throttles raise when the cache is unreachable, turning a
    Redis outage into a 500 on every API request. The storefront's limiter
    already fails open; the API must match."""

    def test_api_serves_requests_when_the_cache_is_down(self):
        from django.core.cache import cache as django_cache

        with patch.object(
            django_cache, "get", side_effect=ConnectionError("redis down")
        ), patch("api.v1.views.list_products", return_value=([make_product()], False)):
            response = self.client.get("/api/v1/products/")
        self.assertEqual(response.status_code, 200)

    def test_fail_open_is_logged_for_alerting(self):
        from api.throttling import AnonRateThrottle

        throttle = AnonRateThrottle()
        with patch.object(
            AnonRateThrottle, "get_cache_key", side_effect=ConnectionError("redis down")
        ), self.assertLogs("api.throttling", level="ERROR") as captured:
            self.assertTrue(throttle.allow_request(None, None))
        self.assertEqual(captured.records[0].event, "throttle_fail_open")


class TokenIssueHardeningTests(ApiTestCase):
    """The token endpoint accepts a password, so it must carry the same
    protections as the HTML login - not a weaker subset."""

    def _post(self, password="S3curePass!x", **extra):
        return self.client.post(
            "/api/v1/auth/token/",
            {"username": "alice", "password": password, **extra},
            content_type="application/json",
        )

    def test_valid_credentials_return_an_access_and_refresh_pair(self):
        body = self._post().json()
        self.assertTrue(body["access"])
        self.assertTrue(body["refresh"])
        self.assertEqual(body["token_type"], "Token")
        self.assertEqual(body["expires_in"], 900)

    def test_brute_force_locks_the_account_across_many_ips(self):
        # A distributed run never trips a per-IP throttle; the per-username
        # lockout is what stops it.
        for i in range(10):
            response = self.client.post(
                "/api/v1/auth/token/",
                {"username": "alice", "password": "wrong"},
                content_type="application/json",
                REMOTE_ADDR=f"198.51.100.{i}",
            )
            self.assertEqual(response.status_code, 400)

        # Even the CORRECT password is now refused, from a fresh IP
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "alice", "password": "S3curePass!x"},
            content_type="application/json",
            REMOTE_ADDR="203.0.113.99",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "account_locked")

    def test_lockout_is_shared_with_the_html_login(self):
        # Failures against the API must lock the browser login too, and
        # notify the owner exactly once.
        from django.core import mail

        for i in range(10):
            self.client.post(
                "/api/v1/auth/token/",
                {"username": "alice", "password": "wrong"},
                content_type="application/json",
                REMOTE_ADDR=f"198.51.100.{i}",
            )
        response = self.client.post(
            "/accounts/login/",
            {"username": "alice", "password": "S3curePass!x"},
            REMOTE_ADDR="203.0.113.50",
        )
        self.assertEqual(response.status_code, 429)
        self.assertEqual(len([m for m in mail.outbox if "locked" in m.subject]), 1)

    def test_per_ip_rate_limit_applies_to_token_issuance(self):
        for _ in range(5):
            self.client.post(
                "/api/v1/auth/token/",
                {"username": "bob", "password": "wrong"},
                content_type="application/json",
            )
        response = self.client.post(
            "/api/v1/auth/token/",
            {"username": "bob", "password": "wrong"},
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 429)

    def test_successful_login_clears_the_failure_count(self):
        from accounts.services.lockout import is_locked

        for i in range(3):
            self.client.post(
                "/api/v1/auth/token/",
                {"username": "alice", "password": "wrong"},
                content_type="application/json",
                REMOTE_ADDR=f"198.51.100.{i}",
            )
        self.assertEqual(self._post().status_code, 200)
        self.assertFalse(is_locked("alice"))

    def test_unverified_email_cannot_obtain_a_token(self):
        Profile.objects.filter(user=self.user).update(email_verified=False)
        response = self._post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "email_not_verified")

    def test_mfa_account_requires_a_one_time_code(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        response = self._post()
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "otp_required")

    def test_mfa_account_rejects_a_wrong_one_time_code(self):
        from django_otp.plugins.otp_totp.models import TOTPDevice

        TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        response = self._post(otp="000000")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "otp_invalid")

    def test_mfa_account_succeeds_with_a_valid_code(self):
        from django_otp.oath import totp
        from django_otp.plugins.otp_totp.models import TOTPDevice

        device = TOTPDevice.objects.create(user=self.user, name="default", confirmed=True)
        code = totp(device.bin_key, device.step, device.t0, device.digits, device.drift)
        response = self._post(otp=str(code).zfill(device.digits))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["access"])


class AccessTokenTests(ApiTestCase):
    """Access tokens are signed and short-lived; nothing is stored."""

    def test_access_token_is_not_persisted_anywhere(self):
        access, _ = issue_access_token(self.user)
        self.assertEqual(RefreshToken.objects.filter(token_hash=access).count(), 0)

    def test_tampered_token_is_rejected(self):
        access, _ = issue_access_token(self.user)
        response = self.client.get(
            "/api/v1/profile/", HTTP_AUTHORIZATION=f"Token {access[:-2]}xy"
        )
        self.assertEqual(response.status_code, 401)

    def test_expired_access_token_is_rejected(self):
        access, _ = issue_access_token(self.user)
        with override_settings(API_ACCESS_TOKEN_TTL_SECONDS=-1):
            response = self.client.get(
                "/api/v1/profile/", HTTP_AUTHORIZATION=f"Token {access}"
            )
        self.assertEqual(response.status_code, 401)

    def test_revocation_invalidates_outstanding_access_tokens(self):
        from api.models import revoke_all_tokens

        access, _ = issue_access_token(self.user)
        self.assertEqual(
            self.client.get(
                "/api/v1/profile/", HTTP_AUTHORIZATION=f"Token {access}"
            ).status_code,
            200,
        )
        revoke_all_tokens(self.user)
        self.assertEqual(
            self.client.get(
                "/api/v1/profile/", HTTP_AUTHORIZATION=f"Token {access}"
            ).status_code,
            401,
        )


class RefreshTokenRotationTests(ApiTestCase):
    def _login(self):
        return self.client.post(
            "/api/v1/auth/token/",
            {"username": "alice", "password": "S3curePass!x"},
            content_type="application/json",
        ).json()

    def _refresh(self, token):
        return self.client.post(
            "/api/v1/auth/token/refresh/",
            {"refresh": token},
            content_type="application/json",
        )

    def test_refresh_returns_a_new_pair_and_retires_the_old_token(self):
        first = self._login()
        response = self._refresh(first["refresh"])
        self.assertEqual(response.status_code, 200)
        second = response.json()
        self.assertNotEqual(second["refresh"], first["refresh"])
        self.assertTrue(second["access"])

    def test_only_refresh_tokens_are_stored_and_only_as_digests(self):
        body = self._login()
        stored = RefreshToken.objects.get(used_at__isnull=True)
        self.assertEqual(stored.token_hash, hash_refresh_token(body["refresh"]))
        self.assertNotIn(body["refresh"], stored.token_hash)

    def test_replaying_a_rotated_token_revokes_the_whole_family(self):
        first = self._login()
        second = self._refresh(first["refresh"]).json()

        # The stolen (already rotated) token is replayed
        replay = self._refresh(first["refresh"])
        self.assertEqual(replay.status_code, 401)
        self.assertEqual(replay.json()["error"]["code"], "invalid_refresh_token")

        # ...and the legitimate client's token is revoked too: theft ends
        # the session rather than letting it continue silently.
        self.assertEqual(self._refresh(second["refresh"]).status_code, 401)

    def test_unknown_refresh_token_is_rejected(self):
        self.assertEqual(self._refresh("not-a-real-token").status_code, 401)

    def test_revoking_invalidates_refresh_tokens(self):
        body = self._login()
        response = self.client.delete(
            "/api/v1/auth/token/", HTTP_AUTHORIZATION=f"Token {body['access']}"
        )
        self.assertEqual(response.status_code, 204)
        self.assertEqual(self._refresh(body["refresh"]).status_code, 401)
