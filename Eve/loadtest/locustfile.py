"""Eve staging load test — see README.md.

Six flows, weighted to approximate real traffic:
  landing / catalogue browsing, product detail (cache hit + miss),
  login + authenticated session, cart mutations, checkout, webhook bursts.

The run fails (non-zero exit) when the SLOs in docs/OBSERVABILITY.md are
missed, so it is usable as a release gate.
"""
import json
import os
import random
import sys
import uuid

import gevent
from locust import HttpUser, between, events, tag, task
from locust.exception import StopUser

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import DEFAULT_MANIFEST_PATH  # noqa: E402 - sibling module
from signing import SaleorSigner  # noqa: E402 — sibling module, path set above

MANIFEST_PATH = os.environ.get("LOADTEST_MANIFEST", DEFAULT_MANIFEST_PATH)
# Checkout POSTs create real Saleor checkouts — opt in explicitly
CHECKOUT_MODE = os.environ.get("LOADTEST_CHECKOUT", "guard")  # guard | full

# SLO budgets in ms, as (p50, p95, p99) per request name.
# p50 catches a slow baseline, p95 is the headline SLO
# (docs/OBSERVABILITY.md), p99 catches tail latency that p95 hides.
SLO_BUDGETS_MS = {
    "GET /":                            (200, 500, 1000),
    "GET /shop/catalogue/":             (200, 500, 1000),
    "GET /shop/product/[hit]":          (200, 500, 1000),
    # A miss is allowed one upstream Saleor call
    "GET /shop/product/[miss]":         (400, 800, 1500),
    # Login is dominated by PBKDF2 password hashing (deliberately slow)
    "POST /accounts/login/":            (600, 1000, 1500),
    "POST /shop/cart/add/":             (200, 500, 1000),
    "GET /shop/cart/":                  (200, 500, 1000),
    "POST /payments/webhooks/saleor/":  (150, 300, 600),
}
MAX_FAILURE_RATIO = 0.01


def load_manifest():
    try:
        with open(MANIFEST_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        raise SystemExit(
            f"Manifest {MANIFEST_PATH!r} not found. Run on staging first:\n"
            "  python manage.py loadtest_seed --with-webhook-key "
            "--output loadtest/manifest.json"
        ) from None


MANIFEST = load_manifest()
# Seeded synthetic product: makes the cache-hit path deterministic instead
# of depending on whatever the staging catalogue happens to hold.
KNOWN_SLUG = os.environ.get("LOADTEST_SLUG") or MANIFEST["slug"]


SIGNER = SaleorSigner(MANIFEST["webhook"]) if MANIFEST.get("webhook") else None


class BrowsingUser(HttpUser):
    """Anonymous traffic: landing, catalogue, product detail hits and misses."""

    weight = 6
    wait_time = between(1, 4)

    @tag("browse")
    @task(5)
    def landing(self):
        self.client.get("/", name="GET /")

    @tag("browse")
    @task(8)
    def catalogue(self):
        self.client.get("/shop/catalogue/", name="GET /shop/catalogue/")

    @tag("browse", "cache")
    @task(6)
    def product_detail_cache_hit(self):
        # Same slug every time: served from the Mongo product cache
        self.client.get(f"/shop/product/{KNOWN_SLUG}/", name="GET /shop/product/[hit]")

    @tag("browse", "cache")
    @task(1)
    def product_detail_cache_miss(self):
        # Fresh slug every time: exercises the miss path and the negative
        # cache added for threat-model R3. 404 is the expected outcome.
        slug = f"loadtest-miss-{uuid.uuid4().hex[:12]}"
        with self.client.get(
            f"/shop/product/{slug}/", name="GET /shop/product/[miss]",
            catch_response=True,
        ) as response:
            if response.status_code == 404:
                response.success()


class ShopperUser(HttpUser):
    """Authenticated session: login, cart mutations, checkout."""

    weight = 3
    wait_time = between(2, 6)

    def on_start(self):
        self.username = random.choice(MANIFEST["users"])
        self.logged_in = self._login()

    def _csrf(self, path, name):
        """Fetch a page for its CSRF token, recorded under a stable name so
        parameterised URLs don't fragment the statistics."""
        self.client.get(path, name=name)
        return self.client.cookies.get("csrftoken", "")

    def _login(self):
        token = self._csrf("/accounts/login/", "GET /accounts/login/")
        with self.client.post(
            "/accounts/login/",
            data={"username": self.username, "password": MANIFEST["password"],
                  "csrfmiddlewaretoken": token},
            headers={"Referer": self.client.base_url},
            name="POST /accounts/login/", catch_response=True,
        ) as response:
            if response.status_code == 429:
                # Rate limited: raise RATE_LIMIT_SCALE on staging
                response.failure("throttled (raise RATE_LIMIT_SCALE)")
                return False
            response.success()
            return "_auth_user_id" in str(self.client.cookies) or response.ok

    @tag("cart")
    @task(5)
    def add_to_cart(self):
        if not self.logged_in:
            return
        token = self._csrf(f"/shop/product/{KNOWN_SLUG}/", "GET /shop/product/[hit]")
        self.client.post(
            f"/shop/cart/add/{KNOWN_SLUG}/",
            data={"quantity": random.randint(1, 3), "csrfmiddlewaretoken": token},
            headers={"Referer": self.client.base_url},
            name="POST /shop/cart/add/",
        )

    @tag("cart")
    @task(4)
    def view_cart(self):
        if not self.logged_in:
            return
        self.client.get("/shop/cart/", name="GET /shop/cart/")

    @tag("cart")
    @task(1)
    def remove_from_cart(self):
        if not self.logged_in:
            return
        token = self._csrf("/shop/cart/", "GET /shop/cart/")
        self.client.post(
            "/shop/cart/remove/loadtest-nonexistent/",
            data={"csrfmiddlewaretoken": token},
            headers={"Referer": self.client.base_url},
            name="POST /shop/cart/remove/",
        )

    @tag("checkout")
    @task(2)
    def checkout(self):
        if not self.logged_in:
            return
        self.client.get("/payments/checkout/", name="GET /payments/checkout/")
        if CHECKOUT_MODE != "full":
            return  # guard mode: measure the page, never place orders
        token = self.client.cookies.get("csrftoken", "")
        self.client.post(
            "/payments/checkout/",
            data={"csrfmiddlewaretoken": token},
            headers={"Referer": self.client.base_url},
            name="POST /payments/checkout/",
        )

    @tag("checkout")
    @task(1)
    def order_history(self):
        if not self.logged_in:
            return
        self.client.get("/payments/history/", name="GET /payments/history/")


class WebhookBurstUser(HttpUser):
    """Saleor delivering signed order events, in bursts rather than evenly —
    verification (JWKS + RSA) and the row-locked update are the hot path."""

    weight = 1
    wait_time = between(5, 10)
    BURST_SIZE = 10

    def on_start(self):
        if SIGNER is None:
            # Manifest seeded without --with-webhook-key: skip this stage
            # instead of aborting the whole run
            print("No webhook key in the manifest — skipping the webhook stage "
                  "(re-seed with --with-webhook-key to include it).")
            raise StopUser()
        self.order_ids = list(MANIFEST["order_ids"])
        random.shuffle(self.order_ids)

    def _deliver(self, payload):
        body, signature = SIGNER.sign(payload)
        with self.client.post(
            "/payments/webhooks/saleor/", data=body,
            headers={"Content-Type": "application/json",
                     "Saleor-Signature": signature},
            name="POST /payments/webhooks/saleor/", catch_response=True,
        ) as response:
            if response.status_code == 401:
                response.failure("signature rejected (JWKS key expired? re-seed)")
            elif response.status_code in (200, 409):
                response.success()  # 409 = refused invalid transition, by design

    @tag("webhook")
    @task
    def burst(self):
        order_id = random.choice(self.order_ids)
        # A burst of concurrent deliveries: a real transition plus
        # re-deliveries of the same event (idempotency path)
        greenlets = [
            gevent.spawn(self._deliver,
                         {"__typename": "OrderFullyPaid", "order": {"id": order_id}})
            for _ in range(self.BURST_SIZE)
        ]
        gevent.joinall(greenlets)


@events.quitting.add_listener
def enforce_slos(environment, **kwargs):
    """Fail the run when SLOs are missed, so CI/release gates can rely on it."""
    stats = environment.stats
    failures = []

    ratio = stats.total.fail_ratio
    if ratio > MAX_FAILURE_RATIO:
        failures.append(f"failure ratio {ratio:.2%} > {MAX_FAILURE_RATIO:.2%}")

    for name, budgets in SLO_BUDGETS_MS.items():
        entry = next(
            (e for e in stats.entries.values() if e.name == name and e.num_requests),
            None,
        )
        if entry is None:
            continue  # flow not exercised in this run
        for quantile, budget_ms in zip((0.50, 0.95, 0.99), budgets, strict=True):
            observed = entry.get_response_time_percentile(quantile)
            if observed > budget_ms:
                failures.append(
                    f"{name} p{int(quantile * 100)} {observed:.0f}ms > {budget_ms}ms"
                )

    if failures:
        environment.process_exit_code = 1
        print("\nSLO FAILURES:")
        for failure in failures:
            print(f"  - {failure}")
    else:
        environment.process_exit_code = 0
        print("\nAll SLOs met.")
