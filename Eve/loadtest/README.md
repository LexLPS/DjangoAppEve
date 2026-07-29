# Staging load test

Repeatable load test for the go-live gate in docs/RELEASE.md. Covers landing
and catalogue browsing, product-detail cache hits and misses, login and
authenticated sessions, cart mutations, checkout, and signed webhook bursts.
The run exits non-zero when the SLOs in docs/OBSERVABILITY.md are missed, so
it can gate a release.

**Staging only.** Never point this at production: it creates users and
orders, and the webhook stage installs a throwaway signing key.

## 1. Seed staging

On the staging host (or any shell with staging's `DJANGO_ENV`/database):

```bash
python manage.py loadtest_seed --users 20 --with-webhook-key --output loadtest/manifest.json
```

This creates `loadtest_*` users (email pre-verified, because checkout
requires it), pending orders for the webhook stage, and — with
`--with-webhook-key` — a throwaway RSA key placed in the JWKS cache so the
generator can produce signatures Saleor's verifier accepts. The key expires
with `SALEOR_JWKS_CACHE_SECONDS` (default 1 h); re-seed for longer runs.

The manifest contains a private key. It is gitignored; delete it afterwards.

**The seed command must share the app's cache.** The signing key is placed
in the Django cache, so the command has to run with the same `REDIS_URL` as
the running staging pods. With a per-process LocMem cache (the dev default)
the app never sees the key and every webhook returns 401 — run the webhook
stage against a Redis-backed environment only.

## 2. Raise the rate limits for the run

Per-IP limits (login 5/5 min) would otherwise make the test measure 429s.
On staging only, set `RATE_LIMIT_SCALE=100` and redeploy. A deploy check
(`eve.W002`) fails the release pipeline if this ever reaches production.

## 3. Run

```bash
pip install -r loadtest/requirements.txt
locust -f loadtest/locustfile.py --host https://staging.example.com \
  --users 200 --spawn-rate 10 --run-time 10m --headless \
  --csv results/run-$(date +%Y%m%d-%H%M)
```

Repeatability: same seed size, same `--users/--spawn-rate/--run-time`, and
the `--csv` artifacts archived per run. Ramp to **2× expected peak** for the
go-live gate.

| Env var | Default | Meaning |
|---|---|---|
| `LOADTEST_MANIFEST` | `loadtest/manifest.json` | path to the seed manifest |
| `LOADTEST_SLUG` | `eve-horizon-nature-escape` | a slug that exists in staging (cache-hit path) |
| `LOADTEST_CHECKOUT` | `guard` | `guard` = never POST checkout; `full` = place real Saleor orders |

`LOADTEST_CHECKOUT=full` creates **real Saleor checkouts and orders**. Use it
only against a staging Saleor channel, at low user counts, and clean up
afterwards.

Target a subset with tags: `--tags browse`, `cache`, `cart`, `checkout`,
`webhook`.

## 4. What "pass" means

- Total failure ratio ≤ 1 %.
- p95 within budget per flow (cache hit 500 ms, cache miss 800 ms, login
  800 ms, cart 500 ms, webhook 300 ms).
- No resource exhaustion. Run the sampler alongside the test:

  ```bash
  python manage.py sample_resources --interval 10 --duration 600
  ```

  Watch `pg_total` vs `pg_max`, `redis_in_use` vs `redis_max`,
  `mongo_current`, any `mongo_pool_wait` / `mongo_pool_exhausted` events,
  and `queue_ms` in the `http_request` events (rising queue time with flat
  `duration_ms` = worker saturation). See docs/OBSERVABILITY.md.
- Webhook stage returns 202 only — a 401 means the seeded key expired.

## 5. Clean up

```bash
python manage.py loadtest_seed --cleanup
rm loadtest/manifest.json
```

Then unset `RATE_LIMIT_SCALE` and redeploy. Record the run (date, profile,
results, anomalies) in the release sign-off.
