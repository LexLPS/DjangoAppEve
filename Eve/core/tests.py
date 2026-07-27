from unittest.mock import patch

from django.core.cache import cache
from django.http import HttpResponse
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

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
    def test_healthy_when_backends_reachable(self):
        with patch("ecommerce.services.mongo_client.client") as mongo:
            mongo.admin.command.return_value = {"ok": 1}
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_degraded_without_details_when_mongo_down(self):
        with patch("ecommerce.services.mongo_client.client") as mongo:
            mongo.admin.command.side_effect = RuntimeError("secret connection detail")
            response = self.client.get(reverse("health"))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"status": "degraded"})
        self.assertNotIn("secret connection detail", response.content.decode())
