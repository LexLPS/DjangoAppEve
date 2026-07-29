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
        response = self.csrf_client.post(reverse("login"), {"username": "x", "password": "y"})
        self.assertEqual(response.status_code, 403)

    def test_register_post_without_token_rejected(self):
        response = self.csrf_client.post(reverse("register"), {})
        self.assertEqual(response.status_code, 403)

    def test_contact_post_without_token_rejected(self):
        response = self.csrf_client.post(
            reverse("contact"),
            {
                "name": "a",
                "email": "a@example.com",
                "subject": "s",
                "message": "m",
            },
        )
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
            "/",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="6.6.6.6, 203.0.113.5",
        )
        # The spoofable client-supplied prefix (6.6.6.6) is ignored; only the
        # rightmost untrusted hop — appended by our own proxy — counts
        self.assertEqual(self.middleware(request).content, b"203.0.113.5")

    @override_settings(TRUSTED_PROXIES=["100.64.0.0/10"])
    def test_railway_real_ip_taken_from_trusted_proxy_cidr(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="100.64.0.4",
            HTTP_X_REAL_IP="203.0.113.5",
            HTTP_X_FORWARDED_FOR="6.6.6.6",
        )
        self.assertEqual(self.middleware(request).content, b"203.0.113.5")

    @override_settings(TRUSTED_PROXIES=["100.64.0.0/10"])
    def test_real_ip_ignored_from_untrusted_peer(self):
        request = self.factory.get("/", REMOTE_ADDR="198.51.100.9", HTTP_X_REAL_IP="203.0.113.5")
        self.assertEqual(self.middleware(request).content, b"198.51.100.9")

    @override_settings(TRUSTED_PROXIES=["100.64.0.0/10"])
    def test_malformed_real_ip_does_not_replace_peer(self):
        request = self.factory.get("/", REMOTE_ADDR="100.64.0.4", HTTP_X_REAL_IP="not-an-ip")
        self.assertEqual(self.middleware(request).content, b"100.64.0.4")

    @override_settings(TRUSTED_PROXIES=["10.0.0.1"])
    def test_malformed_forwarded_chain_is_rejected(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="10.0.0.1",
            HTTP_X_FORWARDED_FOR="203.0.113.5, not-an-ip",
        )
        self.assertEqual(self.middleware(request).content, b"10.0.0.1")

    @override_settings(TRUSTED_PROXIES=["10.0.0.1"])
    def test_forwarded_header_ignored_from_untrusted_peer(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="198.51.100.9",
            HTTP_X_FORWARDED_FOR="203.0.113.5",
        )
        self.assertEqual(self.middleware(request).content, b"198.51.100.9")

    def test_noop_when_no_trusted_proxies_configured(self):
        request = self.factory.get(
            "/",
            REMOTE_ADDR="198.51.100.9",
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
        response = self.client.post(reverse("login"), {"username": "x", "password": "y"})
        self.assertEqual(response.status_code, 429)


class SafeJSONSerializerTests(TestCase):
    """Redis cache values must round-trip as JSON, never pickle (R2)."""

    def setUp(self):
        from .cache import SafeJSONSerializer

        self.serializer = SafeJSONSerializer()

    def test_round_trips_json_shapes(self):
        for value in ({"a": [1, 2]}, "text", True, None, 3.5, ["x"]):
            self.assertEqual(self.serializer.loads(self.serializer.dumps(value)), value)

    def test_integers_pass_through_raw_for_incr(self):
        self.assertEqual(self.serializer.dumps(7), 7)  # not bytes: INCR-able
        self.assertEqual(self.serializer.loads(b"7"), 7)

    def test_booleans_are_not_treated_as_raw_integers(self):
        dumped = self.serializer.dumps(True)
        self.assertIsInstance(dumped, bytes)
        self.assertIs(self.serializer.loads(dumped), True)

    def test_never_deserializes_pickle(self):
        import pickle

        # Pickle bytes are rejected as malformed JSON, never executed
        with self.assertRaises(ValueError):
            self.serializer.loads(pickle.dumps({"evil": True}))


class TrustedProxyDeployCheckTests(TestCase):
    """R1: unset TRUSTED_PROXIES must fail the deploy check, not silently
    collapse per-IP throttling into one shared bucket."""

    def test_warns_when_unset(self):
        from .checks import trusted_proxies_configured

        with override_settings(TRUSTED_PROXIES=[]):
            warnings = trusted_proxies_configured(None)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0].id, "eve.W001")

    def test_silent_when_configured(self):
        from .checks import trusted_proxies_configured

        with override_settings(TRUSTED_PROXIES=["100.64.0.0/10"]):
            self.assertEqual(trusted_proxies_configured(None), [])


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
        with (
            patch("ecommerce.services.mongo_client.client") as mongo,
            patch("ecommerce.services.saleor_client._circuit_is_open", return_value=True),
        ):
            mongo.admin.command.return_value = {"ok": 1}
            response = self.client.get(reverse("readiness"))
        self.assertEqual(response.status_code, 200)  # Saleor is a soft dependency
        self.assertEqual(response.json()["saleor_circuit"], "open")


class ObservabilityTests(TestCase):
    def test_every_response_carries_a_unique_request_id(self):
        first = self.client.get(reverse("landing"))
        second = self.client.get(reverse("landing"))
        self.assertTrue(first.headers["X-Request-ID"])
        self.assertNotEqual(first.headers["X-Request-ID"], second.headers["X-Request-ID"])

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


class QueueTimeTests(TestCase):
    """Time queued before a worker picks the request up — the worker
    saturation signal (docs/OBSERVABILITY.md)."""

    def test_queue_time_reported_when_proxy_sets_header(self):
        import time as pytime

        start = pytime.time() - 0.25  # request accepted 250ms ago
        with self.assertLogs("eve.requests", level="INFO") as captured:
            self.client.get(reverse("landing"), HTTP_X_REQUEST_START=f"t={start:.3f}")
        record = captured.records[0]
        self.assertGreater(record.queue_ms, 100)

    def test_absent_header_omits_the_field(self):
        with self.assertLogs("eve.requests", level="INFO") as captured:
            self.client.get(reverse("landing"))
        self.assertFalse(hasattr(captured.records[0], "queue_ms"))

    def test_malformed_or_skewed_values_are_ignored(self):
        import time as pytime

        for header in (
            "garbage",
            "t=not-a-number",
            f"t={pytime.time() + 60:.3f}",
        ):  # clock skew: future
            with self.assertLogs("eve.requests", level="INFO") as captured:
                self.client.get(reverse("landing"), HTTP_X_REQUEST_START=header)
            self.assertFalse(hasattr(captured.records[0], "queue_ms"), header)


class ResourceSnapshotTests(TestCase):
    """Pool telemetry must never break the caller, even when a backend is
    unreachable."""

    def test_every_backend_reports_stats_or_an_error_never_raises(self):
        # Backends absent under the test settings (SQLite, LocMem) must be
        # reported as errors rather than breaking the sampler
        from .monitoring import snapshot_resources

        stats = snapshot_resources()
        for prefix in ("pg", "redis", "mongo"):
            reported = [key for key in stats if key.startswith(f"{prefix}_")]
            self.assertTrue(reported, f"{prefix} produced no keys: {stats}")

    def test_snapshot_is_logged_as_a_structured_event(self):
        from .monitoring import log_resource_snapshot

        with self.assertLogs("eve.resources", level="INFO") as captured:
            log_resource_snapshot()
        self.assertEqual(captured.records[0].event, "resource_snapshot")

    def test_snapshot_reports_uncertain_checkout_backlog(self):
        from django.contrib.auth.models import User
        from payments.models import CheckoutAttempt

        from .monitoring import _celery_stats

        user = User.objects.create_user("checkout-metrics", "metrics@example.com")
        CheckoutAttempt.objects.create(
            user=user,
            idempotency_key="metrics-attempt",
            cart_fingerprint="a" * 64,
            state=CheckoutAttempt.State.UNKNOWN,
        )
        with patch("redis.Redis.from_url", side_effect=ConnectionError):
            stats = _celery_stats()
        self.assertEqual(stats["checkout_uncertain"], 1)
        self.assertGreaterEqual(stats["checkout_oldest_uncertain_seconds"], 0)


class MongoPoolListenerTests(TestCase):
    def test_slow_checkout_is_logged_with_wait_time(self):
        from unittest.mock import Mock

        from .monitoring import MongoPoolLogger

        listener = MongoPoolLogger()
        with self.assertLogs("eve.resources", level="WARNING") as captured:
            listener.connection_checked_out(Mock(duration=0.4))  # 400ms wait
        record = captured.records[0]
        self.assertEqual(record.event, "mongo_pool_wait")
        self.assertAlmostEqual(record.wait_ms, 400, delta=1)

    def test_fast_checkout_is_not_logged(self):
        from unittest.mock import Mock

        from .monitoring import MongoPoolLogger

        listener = MongoPoolLogger()
        with self.assertNoLogs("eve.resources", level="WARNING"):
            listener.connection_checked_out(Mock(duration=0.001))

    def test_pool_exhaustion_is_logged(self):
        from unittest.mock import Mock

        from .monitoring import MongoPoolLogger

        listener = MongoPoolLogger()
        with self.assertLogs("eve.resources", level="ERROR") as captured:
            listener.connection_check_out_failed(Mock(reason="timeout"))
        self.assertEqual(captured.records[0].event, "mongo_pool_exhausted")


class LogRedactionTests(TestCase):
    def _formatted(self, message):
        import logging as pylogging

        from .logging import JsonFormatter, RequestIdFilter, SensitiveDataFilter

        record = pylogging.LogRecord("test", pylogging.INFO, __file__, 1, message, (), None)
        RequestIdFilter().filter(record)
        SensitiveDataFilter().filter(record)
        return JsonFormatter().format(record)

    def test_bearer_tokens_and_passwords_redacted(self):
        output = self._formatted("retry with Authorization: Bearer sk_live_abc123 password=hunter2")
        self.assertNotIn("sk_live_abc123", output)
        self.assertNotIn("hunter2", output)
        self.assertIn("[REDACTED]", output)

    def test_session_cookies_and_pans_redacted(self):
        output = self._formatted("cookie: sessionid=abc123def card 4111111111111111 declined")
        self.assertNotIn("abc123def", output)
        self.assertNotIn("4111111111111111", output)

    def test_output_is_valid_json_with_correlation_id(self):
        import json as pyjson

        payload = pyjson.loads(self._formatted("plain message"))
        self.assertEqual(payload["message"], "plain message")
        self.assertEqual(payload["level"], "INFO")
        self.assertIn("request_id", payload)

    def _formatted_exception(self, formatter, exc_message):
        import logging as pylogging
        import sys

        from .logging import RequestIdFilter

        try:
            raise RuntimeError(exc_message)
        except RuntimeError:
            exc_info = sys.exc_info()
        record = pylogging.LogRecord(
            "test",
            pylogging.ERROR,
            __file__,
            1,
            "upstream call failed",
            (),
            exc_info,
        )
        RequestIdFilter().filter(record)
        return formatter.format(record)

    def test_exception_tracebacks_redacted_in_json_formatter(self):
        # Exception text bypasses logging filters; the formatter must scrub it
        from .logging import JsonFormatter

        output = self._formatted_exception(
            JsonFormatter(),
            "refused with Authorization: Bearer sk_live_xyz "
            "Body starts with: '<html>internal secret'",
        )
        self.assertNotIn("sk_live_xyz", output)
        self.assertNotIn("internal secret", output)
        self.assertIn("[REDACTED]", output)

    def test_exception_tracebacks_redacted_in_console_formatter(self):
        from .logging import ConsoleFormatter

        output = self._formatted_exception(
            ConsoleFormatter(),
            "sessionid=abc123def Body starts with: '<html>internal secret'",
        )
        self.assertNotIn("abc123def", output)
        self.assertNotIn("internal secret", output)


class RetentionTests(TestCase):
    def test_purge_expired_data_deletes_only_expired_records(self):
        from datetime import timedelta
        from io import StringIO
        from unittest.mock import MagicMock

        from django.core.management import call_command
        from django.utils import timezone
        from payments.models import WebhookEvent

        fresh = ContactMessage.objects.create(
            name="new", email="new@example.com", subject="s", message="m"
        )
        stale = ContactMessage.objects.create(
            name="old", email="old@example.com", subject="s", message="m"
        )
        ContactMessage.objects.filter(pk=stale.pk).update(
            created_at=timezone.now() - timedelta(days=400)
        )
        old_processed = WebhookEvent.objects.create(
            fingerprint="b" * 64,
            event_type="OrderFullyPaid",
            saleor_order_id="OLD",
            payload={},
            status=WebhookEvent.Status.PROCESSED,
        )
        old_pending = WebhookEvent.objects.create(
            fingerprint="c" * 64,
            event_type="OrderFullyPaid",
            saleor_order_id="PENDING",
            payload={},
        )
        WebhookEvent.objects.filter(pk__in=[old_processed.pk, old_pending.pk]).update(
            received_at=timezone.now() - timedelta(days=400)
        )

        with patch("ecommerce.services.mongo_client.carts_collection") as carts:
            carts.delete_many.return_value = MagicMock(deleted_count=3)
            out = StringIO()
            call_command("purge_expired_data", stdout=out)

        self.assertTrue(ContactMessage.objects.filter(pk=fresh.pk).exists())
        self.assertFalse(ContactMessage.objects.filter(pk=stale.pk).exists())
        self.assertFalse(WebhookEvent.objects.filter(pk=old_processed.pk).exists())
        self.assertTrue(WebhookEvent.objects.filter(pk=old_pending.pk).exists())
        cutoff_filter = carts.delete_many.call_args.args[0]
        self.assertIn("$lt", cutoff_filter["updated_at"])
        self.assertIn("3 abandoned cart(s)", out.getvalue())


class LoadTestConfigurationTests(TestCase):
    def test_default_manifest_matches_documented_seed_output(self):
        from loadtest.config import DEFAULT_MANIFEST_PATH

        self.assertEqual(DEFAULT_MANIFEST_PATH, "loadtest/manifest.json")

    def test_offline_gate_accepts_complete_healthy_evidence(self):
        import csv
        import json
        import tempfile
        from pathlib import Path

        from loadtest.config import P95_BUDGETS_MS
        from loadtest.evaluate import evaluate_resources, evaluate_stats

        with tempfile.TemporaryDirectory() as directory:
            stats_path = Path(directory) / "run_stats.csv"
            with stats_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["Name", "Request Count", "Failure Count", "95%"],
                )
                writer.writeheader()
                for name, budget in P95_BUDGETS_MS.items():
                    writer.writerow(
                        {"Name": name, "Request Count": 25, "Failure Count": 0, "95%": budget}
                    )
            resources_path = Path(directory) / "resources.jsonl"
            resources_path.write_text(
                json.dumps(
                    {
                        "event": "resource_snapshot",
                        "pg_total": 4,
                        "pg_max": 20,
                        "redis_in_use": 3,
                        "redis_max": 50,
                        "mongo_current": 2,
                        "mongo_max_pool": 50,
                        "checkout_uncertain": 0,
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(evaluate_stats(stats_path)[0], [])
            self.assertEqual(evaluate_resources(resources_path)[0], [])

    def test_offline_gate_rejects_missing_flow_and_resource_exhaustion(self):
        import csv
        import json
        import tempfile
        from pathlib import Path

        from loadtest.evaluate import evaluate_resources, evaluate_stats

        with tempfile.TemporaryDirectory() as directory:
            stats_path = Path(directory) / "run_stats.csv"
            with stats_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["Name", "Request Count", "Failure Count", "95%"],
                )
                writer.writeheader()
                writer.writerow(
                    {"Name": "GET /", "Request Count": 5, "Failure Count": 1, "95%": 900}
                )
            resources_path = Path(directory) / "resources.jsonl"
            resources_path.write_text(
                json.dumps(
                    {"event": "resource_snapshot", "pg_total": 9, "pg_max": 10}
                ),
                encoding="utf-8",
            )
            stats_failures, _ = evaluate_stats(stats_path)
            resource_failures, _ = evaluate_resources(resources_path)
        self.assertTrue(any("required flow missing" in failure for failure in stats_failures))
        self.assertTrue(any("utilization" in failure for failure in resource_failures))


class DataProtectionAuditTests(TestCase):
    def test_current_automated_evidence_passes_without_printing_timestamps(self):
        from io import StringIO

        from django.core.management import call_command
        from django.utils import timezone

        marker = timezone.now().isoformat()
        output = StringIO()
        with override_settings(
            POSTGRES_BACKUP_LAST_SUCCESS_AT=marker,
            MONGODB_BACKUP_LAST_SUCCESS_AT=marker,
            RESTORE_TEST_LAST_SUCCESS_AT=marker,
            BACKUP_ENCRYPTION_CONFIRMED=True,
            BACKUP_OFFSITE_CONFIRMED=True,
        ):
            call_command("audit_data_protection", stdout=output)
        self.assertIn("audit passed", output.getvalue())
        self.assertNotIn(marker, output.getvalue())

    def test_missing_or_stale_evidence_fails_closed(self):
        from datetime import timedelta
        from io import StringIO

        from django.core.management import CommandError, call_command
        from django.utils import timezone

        stale = (timezone.now() - timedelta(days=200)).isoformat()
        with override_settings(
            POSTGRES_BACKUP_LAST_SUCCESS_AT="",
            MONGODB_BACKUP_LAST_SUCCESS_AT=stale,
            RESTORE_TEST_LAST_SUCCESS_AT=stale,
            BACKUP_ENCRYPTION_CONFIRMED=False,
            BACKUP_OFFSITE_CONFIRMED=False,
        ):
            with self.assertRaises(CommandError):
                call_command("audit_data_protection", stderr=StringIO())

    def test_cache_broker_isolation_deploy_check(self):
        from core.checks import cache_and_broker_are_isolated

        with override_settings(REDIS_URL="rediss://cache", CELERY_BROKER_URL="rediss://cache"):
            warnings = cache_and_broker_are_isolated(None)
        self.assertEqual(warnings[0].id, "eve.W003")


class CelerySecurityConfigurationTests(TestCase):
    def test_only_json_task_messages_are_accepted(self):
        from django.conf import settings

        self.assertEqual(settings.CELERY_TASK_SERIALIZER, "json")
        self.assertEqual(settings.CELERY_ACCEPT_CONTENT, ["json"])
        self.assertTrue(settings.CELERY_TASK_IGNORE_RESULT)

    def test_payment_and_email_tasks_use_separate_queues(self):
        from django.conf import settings

        self.assertEqual(
            settings.CELERY_TASK_ROUTES["payments.tasks.process_webhook_event"]["queue"],
            "webhooks",
        )
        self.assertEqual(settings.CELERY_TASK_ROUTES["accounts.tasks.*"]["queue"], "email")


class CacheLeaseTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_only_one_owner_and_lease_is_released(self):
        from core.cache_lock import cache_lease

        with cache_lease("test-lease", timeout=10) as first:
            with cache_lease("test-lease", timeout=10) as second:
                self.assertTrue(first)
                self.assertFalse(second)
        with cache_lease("test-lease", timeout=10) as third:
            self.assertTrue(third)

    def test_cache_failure_is_explicit(self):
        from core.cache_lock import CacheLeaseUnavailable, cache_lease

        with patch("core.cache_lock.cache.add", side_effect=ConnectionError):
            with self.assertRaises(CacheLeaseUnavailable):
                with cache_lease("test-lease", timeout=10):
                    pass
