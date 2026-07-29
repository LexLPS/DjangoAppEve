# Observability

## Structured logging

- `LOG_FORMAT=json` (production default) emits one JSON object per line:
  `timestamp`, `level`, `logger`, `message`, `request_id`, plus any
  structured extras. Dev keeps human-readable console output.
- **Correlation IDs:** every request gets a server-generated ID, returned as
  `X-Request-ID` and stamped on every log record from that request — join on
  `request_id` to reconstruct a request across log lines. Inbound
  `X-Request-ID` headers are ignored (untrusted).
- **Redaction:** the `SensitiveDataFilter` scrubs credential-shaped content
  (bearer tokens, passwords, secrets, API keys, session/CSRF cookies,
  PAN-like digit runs) from every record as a safety net. The primary rule
  stands: code never logs credentials, cookies, authorization headers,
  health data (hospital/room/patient fields), or payment data. Sentry gets
  the same treatment (`send_default_pii=False` + header/cookie scrubber).

## Metrics (log-derived)

Three structured event families, all plain JSON log lines — no metrics
backend required. Aggregate them in the log platform.

**`http_request`** (one per request, from `RequestMetricsMiddleware`; health
probes and static files excluded): `method`, `route`, `status`,
`duration_ms`, `db_queries`, `db_ms`, and `queue_ms` when the proxy sets
`X-Request-Start` (deploy/nginx.conf does).

**`saleor_call`** (one per upstream call): `outcome` (`ok`, `http_error`,
`timeout`, `connection_error`, `invalid_json`, `graphql_error`,
`circuit_open`, …), `duration_ms`, `attempts`, `status`.
**`saleor_circuit`**: `state` = `open` / `closed`, so an alert on an open
circuit auto-resolves on recovery.

**`resource_snapshot`** (from `manage.py sample_resources`), plus
`mongo_pool_wait` / `mongo_pool_exhausted` emitted live by the pymongo pool
listener.

**`checkout_attempt`** records durable checkout journal state without email,
cart contents, totals, or idempotency tokens. Join it to the originating
request through `request_id`. **`checkout_reconciliation`** is emitted when
attempts remain uncertain beyond the configured recovery grace period.

| Metric | Source |
|---|---|
| Request latency p50 / p95 / p99 | `duration_ms` percentiles per `route` |
| Error rate | share of `http_request` events with `status >= 500` |
| Worker saturation | `queue_ms` — time queued before a worker accepted the request. Rising `queue_ms` with flat `duration_ms` means every worker is busy: add workers or pods. (A true busy-worker gauge needs gunicorn's `--statsd-host` and a statsd sink.) |
| PostgreSQL query time | `db_ms`, `db_queries` per route |
| PostgreSQL connections | `pg_active` / `pg_total` / `pg_max` in `resource_snapshot` |
| Redis pool usage | `redis_in_use` / `redis_available` / `redis_max` |
| MongoDB database size | `mongo_data_mb` / `mongo_storage_mb` / `mongo_index_mb`, plus collection, object, and index counts |
| MongoDB client pool cap | `mongo_max_pool`; live pressure comes from the wait-queue events below because Atlas least-privilege users cannot run cluster-wide `serverStatus` |
| MongoDB wait-queue time | `mongo_pool_wait` events (check-outs ≥ 50 ms) and `mongo_pool_exhausted` on `waitQueueTimeoutMS` expiry |
| Saleor request rate & latency | count and `duration_ms` of `saleor_call` events |
| Saleor availability | `outcome` mix of `saleor_call` + `saleor_circuit` state changes + `saleor_circuit` field in `/healthz/ready/` |
| Queue depth | `queue_<name>_depth` in `resource_snapshot`; durable payment backlog uses `webhook_pending` and `webhook_oldest_seconds` |
| Checkout recovery backlog | `checkout_uncertain` and `checkout_oldest_uncertain_seconds` in `resource_snapshot` |

Requests slower than 1 s log at WARNING (`SLOW_REQUEST_MS`).

### Sampling pool usage

```bash
python manage.py sample_resources --interval 10 --duration 600
```

Run it during load tests to see *which* resource saturates, and on a short
cron in production for a continuous capacity signal.

## Probes

- `GET /healthz/live/` — liveness: process up; no dependency checks. Use as
  the restart probe.
- `GET /healthz/ready/` — readiness: PostgreSQL, MongoDB, and cache checked
  (status per check, never error details) plus the Saleor circuit state.
  Saleor being down does **not** fail readiness — the site degrades
  gracefully. Use as the traffic-gate probe in the LB/orchestrator.
- `GET /healthz/` — legacy alias for readiness.

## Exception monitoring

Set `SENTRY_DSN` to activate Sentry (per environment via `DJANGO_ENV`).
Alert routing: production errors page the on-call; staging errors go to the
team channel. `SENTRY_TRACES_SAMPLE_RATE` (default 0) enables tracing.

## Service-level objectives

Measured over 30 days, on production traffic, excluding health probes:

| SLO | Target |
|---|---|
| Availability (non-5xx responses) | ≥ 99.5 % |
| Latency, p95, HTML routes | ≤ 500 ms |
| Latency, p95, API routes | ≤ 300 ms |
| Checkout success (POST /payments/checkout/ non-5xx while enabled) | ≥ 99 % |
| Webhook processing (2xx to Saleor) | ≥ 99.9 % |

## Alert thresholds (actionable, page-worthy in bold)

- **5xx rate > 1 % over 5 min** — page.
- **`/healthz/ready/` failing ≥ 2 consecutive checks on all pods** — page.
- **Saleor circuit open > 5 min** (`Saleor circuit opened` log event without
  a matching recovery) — page during business hours; catalogue and checkout
  are degraded.
- p95 latency > 2× SLO for 15 min — ticket.
- **`queue_ms` p95 > 100 ms for 10 min** — workers are saturated; scale out.
- `mongo_pool_exhausted` events, or `redis_in_use` sustained near
  `redis_max`, or `pg_total` above 80 % of `pg_max` — ticket; capacity is
  about to become an outage.
- `Rate-limit cache unavailable` or `Lockout cache unavailable` events —
  ticket immediately; brute-force protection is failing open.
- Repeated 429s from one IP or one username lockout burst — security review.
- `Saleor webhook rejected: bad or missing signature` spike — security review.
- **`checkout_uncertain > 0` for 10 min**, or a
  `checkout_reconciliation` event after the hourly repair job, requires
  immediate review. Do not ask the customer to resubmit until reconciliation
  has completed.
- Backup job failure or missed retention run (`purge_expired_data`) — ticket.
