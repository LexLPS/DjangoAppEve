"""Configuration shared by the load-test runner, evaluator, and tests."""

DEFAULT_MANIFEST_PATH = "loadtest/manifest.json"

# Stable request names and their p95 release budgets. Keeping this outside
# locustfile.py lets the offline evaluator run without importing Locust.
P95_BUDGETS_MS = {
    "GET /": 500,
    "GET /shop/catalogue/": 500,
    "GET /shop/product/[hit]": 500,
    "GET /shop/product/[miss]": 800,
    "POST /accounts/login/": 1000,
    "POST /shop/cart/add/": 500,
    "GET /shop/cart/": 500,
    "POST /payments/webhooks/saleor/": 300,
}
MAX_FAILURE_RATIO = 0.01
