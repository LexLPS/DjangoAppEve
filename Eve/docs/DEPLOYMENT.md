# Deployment Architecture

```
client ──TLS──> load balancer / nginx ──> gunicorn (N stateless web pods)
                     │                        │
                     └─ /static/ (nginx,      ├─> PostgreSQL  (orders, payments,
                        object storage,       │    users, sessions — authoritative)
                        or CDN)               ├─> MongoDB     (carts, product cache)
                                              ├─> Redis       (cache, rate limits,
                                              │    session reads)
                                              └─> Saleor      (GraphQL, HTTPS)
```

## Container

- `Eve/Dockerfile` builds a reproducible image: dependencies come only from
  `requirements.lock`, static files are collected at build time, and the app
  runs as an unprivileged user under Gunicorn.
- `docker-compose.yml` runs the full production-shaped stack locally
  (nginx → gunicorn + PostgreSQL, MongoDB, Redis with `volatile-lru`).
- Release steps per deploy: `migrate`, `ensure_indexes`, roll pods.

## Gunicorn

Configured in `gunicorn.conf.py`; tune via environment:

| Variable | Default | Meaning |
|---|---|---|
| `GUNICORN_WORKERS` | `2×CPU+1` | worker processes per pod |
| `GUNICORN_THREADS` | 1 | threads per worker |
| `GUNICORN_FORWARDED_ALLOW_IPS` | `127.0.0.1` | proxies allowed to set forwarded headers |

Workers recycle after ~1000 requests (with jitter) to bound leaks.

## Statelessness

Web pods hold no state: sessions live in PostgreSQL (Redis-accelerated via
`cached_db`), carts in MongoDB, rate-limit counters in Redis, uploads don't
exist, and static files are baked at build time. Any pod can be killed or
cloned at any moment — scale horizontally by adding pods behind the load
balancer.

## Trusted proxies and forwarded headers

Correct client IPs matter here: rate limiting, account lockout, and the
admin IP allowlist all key on `REMOTE_ADDR`.

1. The reverse proxy **must overwrite** (never append blindly to a
   client-supplied value it forwards untouched) `X-Forwarded-Proto`, and
   append the peer address to `X-Forwarded-For`
   (`deploy/nginx.conf` does both).
2. Set `DJANGO_TRUSTED_PROXIES` to the proxy addresses. The
   `TrustedProxyMiddleware` then resolves the real client IP by walking
   `X-Forwarded-For` from the right and skipping trusted hops — spoofed
   prefixes sent by clients are ignored. Unset (default), forwarded headers
   are ignored entirely.
3. `SECURE_PROXY_SSL_HEADER` (prod) trusts `X-Forwarded-Proto: https` — safe
   only because of rule 1. Never enable it with a proxy that passes the
   header through from clients.
4. `GUNICORN_FORWARDED_ALLOW_IPS` must list only the proxy; never `*` on a
   port that anything else can reach.

## Static files

`collectstatic` output ships inside the image at `/app/staticfiles`.
Serve it from the nginx `location /static/` block (compose does this via a
shared volume), or upload it to object storage/CDN in the release pipeline
and point `STATIC_URL` at the CDN origin. Gunicorn never serves static
files.

## Database connections

- **PostgreSQL:** persistent connections (`DB_CONN_MAX_AGE`, default 60 s)
  with health checks. Budget: `pods × workers × 1 connection` must stay
  under `max_connections` with headroom; front with PgBouncer
  (transaction pooling) beyond that.
- **MongoDB:** driver pool capped per process (`MONGODB_MAX_POOL_SIZE`,
  default 50), 5 s server selection, 2 s wait-queue timeout.
- **Redis:** pool capped per process (`REDIS_MAX_CONNECTIONS`, default 50),
  2 s timeouts; see docs/SECURITY_OPERATIONS.md for eviction and failover.

## CORS

There is no separate frontend: everything is same-origin server-rendered
HTML plus a session-authenticated JSON API, so **no CORS headers are set
and none should be added preemptively**. If a separate frontend appears,
add `django-cors-headers` with an explicit `CORS_ALLOWED_ORIGINS` list
(never a wildcard), `CORS_ALLOW_CREDENTIALS = True` only if cookie auth is
kept, and restrict allowed methods/headers to what the frontend uses.

## CI gates

`.github/workflows/ci.yml` runs: the test suite, `manage.py check --deploy
--fail-level WARNING` under production settings, `pip-audit` on the lock
file, `ruff` + `bandit` static analysis, and gitleaks secret scanning.
**Mark all five jobs as required status checks in GitHub branch protection**
so a red check blocks merging — and deploy only from `main`.
