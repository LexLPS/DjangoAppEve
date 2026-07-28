import time
from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from .middleware import TrustedProxyMiddleware
from .models import ContactMessage
from .throttling import rate_limit


class CsrfProtectionTests(TestCase):
    """POSTs without a CSRF token must be rejected on every form endpoint."""

    def setUp(self):
        cache.clear()
        self.csrf_client = Client(enforce_csrf_checks=True)

    def test_login_post_without_token_rejected(self):
        response = self.csrf_client.post(
            reverse("login"), {"username": "x", "password": "y"}
        )
        self.assertEqual(response.status_code, 403)

    def test_register_post_without_token_rejected(self):
        response = self.csrf_client.post(reverse("register"), {})
        self.assertEqual(response.status_code, 403)

    def test_contact_post_without_token_rejected(self):
        response = self.csrf_client.post(reverse("contact"), {
            "name": "a", "email": "a@example.com", "subject": "s", "message": "m",
        })
        self.assertEqual(response.status_code, 403)
        self.assertEqual(ContactMessage.objects.count(), 0)


class SecurityHeadersTests(TestCase):
    def test_headers_present_on_responses(self):
        response = self.client.get(reverse("landing"))
        self.assertIn("Content-Security-Policy", response.headers)
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertIn("script-src 'self'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["Referrer-Policy"], "same-origin")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertIn("Permissions-Policy", response.headers)


class ContactThrottleTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_contact_throttled_after_limit(self):
        for _ in range(5):
            self.client.post(reverse("contact"), {})
        response = self.client.post(reverse("contact"), {})
        self.assertEqual(response.status_code, 429)

    def test_contact_get_never_throttled(self):
        for _ in range(10):
            response = self.client.get(reverse("contact"))
        self.assertEqual(response.status_code, 200)


class RateLimitUnitTests(TestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def test_blocks_posts_over_limit_and_ignores_gets(self):
        @rate_limit("unit-test", limit=2, window_seconds=60)
        def dummy(request):
            return HttpResponse("ok")

        self.assertEqual(dummy(self.factory.post("/")).status_code, 200)
        self.assertEqual(dummy(self.factory.post("/")).status_code, 200)
        self.assertEqual(dummy(self.factory.post("/")).status_code, 429)
        self.assertEqual(dummy(self.factory.get("/")).status_code, 200)


class TrustedProxyMiddlewareTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.middleware = TrustedProxyMiddleware(
            lambda request: HttpResponse(request.META["REMOTE_ADDR"])
        )

    @override_settings(TRUSTED_PROXIES=["10.0.0.1"])
    def test_client_ip_taken_from_forwarded_header_behind_trusted_proxy(self):
        request = self.factory.get(
            "/", REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5",
        )
        # The spoofable client-supplied prefix (6.6.6.6) is ignored; only the
        # rightmost untrusted hop — appended by our own proxy — counts
        self.assertEqual(self.middleware(request).content, b"203.0.113.5")

    @override_settings(TRUSTED_PROXIES=["10.0.0.1"])
    def test_forwarded_header_ignored_from_untrusted_peer(self):
        request = self.factory.get(
            "/", REMOTE_ADDR="198.51.100.9",
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        self.assertEqual(self.middleware(request).content, b"198.51.100.9")

    def test_noop_when_no_trusted_proxies_configured(self):
        request = self.factory.get(
            "/", REMOTE_ADDR="198.51.100.9",
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        self.assertEqual(self.middleware(request).content, b"198.51.100.9")


class SharedCacheRateLimitTests(TestCase):
    """Rate-limit counters live in the shared Django cache (Redis in
    production), so attempts made through OTHER workers count here too —
    simulated by writing the counter directly."""

    def setUp(self):
        cache.clear()

    def test_counts_from_other_workers_are_honored(self):
        # login limit is 5/300s; pretend other workers already saw 5 POSTs
        for window in (int(time.time() // 300), int(time.time() // 300) + 1):
            cache.set(f"ratelimit:login:127.0.0.1:{window}", 5, timeout=300)
        response = self.client.post(
            reverse("login"), {"username": "x", "password": "y"}
        )
        self.assertEqual(response.status_code, 429)


class AdminIPAllowlistTests(TestCase):
    @override_settings(ADMIN_ALLOWED_IPS=["203.0.113.10"])
    def test_admin_hidden_from_unlisted_ips(self):
        response = self.client.get("/admin/", REMOTE_ADDR="198.51.100.7")
        self.assertEqual(response.status_code, 404)

    @override_settings(ADMIN_ALLOWED_IPS=["203.0.113.10"])
    def test_admin_reachable_from_allowed_ip(self):
        response = self.client.get("/admin/", REMOTE_ADDR="203.0.113.10")
        self.assertEqual(response.status_code, 302)  # redirect to admin login

    @override_settings(ADMIN_ALLOWED_IPS=["203.0.113.10"])
    def test_non_admin_paths_unaffected(self):
        response = self.client.get(reverse("landing"), REMOTE_ADDR="198.51.100.7")
        self.assertEqual(response.status_code, 200)

    def test_allowlist_disabled_when_empty(self):
        response = self.client.get("/admin/", REMOTE_ADDR="198.51.100.7")
        self.assertEqual(response.status_code, 302)


class HealthCheckTests(TestCase):
    def test_liveness_has_no_dependencies(self):
        with patch("ecommerce.services.mongo_client.client") as mongo:
            mongo.admin.command.side_effect = RuntimeError("db down")
            response = self.client.get(reverse("liveness"))
        self.assertEqual(response.status_code, 200)  # process is alive regardless
        self.assertEqual(response.json(), {"status": "alive"})

    def test_readiness_ok_when_backends_reachable(self):
        with patch("ecommerce.services.mongo_client.client") as mongo:
            mongo.admin.command.return_value = {"ok": 1}
            response = self.client.get(reverse("readiness"))
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["checks"]["postgresql"], "ok")
        self.assertEqual(payload["checks"]["cache"], "ok")
        self.assertIn(payload["saleor_circuit"], ("open", "closed"))

    def test_readiness_degraded_without_details_when_mongo_down(self):
        with patch("ecommerce.services.mongo_client.client") as mongo:
            mongo.admin.command.side_effect = RuntimeError("secret connection detail")
            response = self.client.get(reverse("readiness"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["checks"]["mongodb"], "fail")
        self.assertNotIn("secret connection detail", response.content.decode())

    def test_saleor_circuit_state_does_not_fail_readiness(self):
        with patch("ecommerce.services.mongo_client.client") as mongo, \
             patch("ecommerce.services.saleor_client._circuit_is_open",
                   return_value=True):
            mongo.admin.command.return_value = {"ok": 1}
            response = self.client.get(reverse("readiness"))
        self.assertEqual(response.status_code, 200)  # Saleor is a soft dependency
        self.assertEqual(response.json()["saleor_circuit"], "open")


class ObservabilityTests(TestCase):
    def test_every_response_carries_a_unique_request_id(self):
        first = self.client.get(reverse("landing"))
        second = self.client.get(reverse("landing"))
        self.assertTrue(first.headers["X-Request-ID"])
        self.assertNotEqual(first.headers["X-Request-ID"],
                            second.headers["X-Request-ID"])

    def test_request_metrics_event_logged_with_latency_and_db_stats(self):
        with self.assertLogs("eve.requests", level="INFO") as captured:
            self.client.get(reverse("landing"))
        record = captured.records[0]
        self.assertEqual(record.event, "http_request")
        self.assertEqual(record.status, 200)
        self.assertGreaterEqual(record.duration_ms, 0)
        self.assertGreaterEqual(record.db_queries, 0)

    def test_health_probes_are_not_metric_noise(self):
        with patch("ecommerce.services.mongo_client.client") as mongo:
            mongo.admin.command.return_value = {"ok": 1}
            with self.assertNoLogs("eve.requests", level="INFO"):
                self.client.get(reverse("readiness"))


class LogRedactionTests(TestCase):
    def _formatted(self, message):
        import logging as pylogging

        from .logging import JsonFormatter, RequestIdFilter, SensitiveDataFilter
        record = pylogging.LogRecord(
            "test", pylogging.INFO, __file__, 1, message, (), None
        )
        RequestIdFilter().filter(record)
        SensitiveDataFilter().filter(record)
        return JsonFormatter().format(record)

    def test_bearer_tokens_and_passwords_redacted(self):
        output = self._formatted(
            "retry with Authorization: Bearer sk_live_abc123 password=hunter2"
        )
        self.assertNotIn("sk_live_abc123", output)
        self.assertNotIn("hunter2", output)
        self.assertIn("[REDACTED]", output)

    def test_session_cookies_and_pans_redacted(self):
        output = self._formatted(
            "cookie: sessionid=abc123def card 4111111111111111 declined"
        )
        self.assertNotIn("abc123def", output)
        self.assertNotIn("4111111111111111", output)

    def test_output_is_valid_json_with_correlation_id(self):
        import json as pyjson
        payload = pyjson.loads(self._formatted("plain message"))
        self.assertEqual(payload["message"], "plain message")
        self.assertEqual(payload["level"], "INFO")
        self.assertIn("request_id", payload)


class RetentionTests(TestCase):
    def test_purge_expired_data_deletes_only_expired_records(self):
        from datetime import timedelta
        from io import StringIO
        from unittest.mock import MagicMock

        from django.core.management import call_command
        from django.utils import timezone

        fresh = ContactMessage.objects.create(
            name="new", email="new@example.com", subject="s", message="m")
        stale = ContactMessage.objects.create(
            name="old", email="old@example.com", subject="s", message="m")
        ContactMessage.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(days=400))

        with patch("ecommerce.services.mongo_client.carts_collection") as carts:
            carts.delete_many.return_value = MagicMock(deleted_count=3)
            out = StringIO()
            call_command("purge_expired_data", stdout=out)

        self.assertTrue(ContactMessage.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(ContactMessage.objects.filter(pk=stale.pk).exists())
        cutoff_filter = carts.delete_many.call_args.args[0]
        self.assertIn("$lt", cutoff_filter["updated_at"])
        self.assertIn("3 abandoned cart(s)", out.getvalue())
