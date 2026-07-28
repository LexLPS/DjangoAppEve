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

`RequestMetricsMiddleware` emits one `http_request` event per request with
`method`, `route`, `status`, `duration_ms`, `db_queries`, `db_ms`. Health
probes and static files are excluded. Derive in the log platform:

| Metric | Source |
|---|---|
| Request latency (p50/p95/p99) | `duration_ms` percentiles per `route` |
| Error rate | share of events with `status >= 500` |
| Database performance | `db_ms` and `db_queries` per route; alert on growth |
| Saleor availability | `saleor_circuit` in `/healthz/ready/` + circuit open/close log events |
| Queue depth | n/a today — no async queue exists. If Celery/RQ is added, export queue length and oldest-job age from Redis before go-live |

Requests slower than 1 s log at WARNING (`SLOW_REQUEST_MS`).

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
- `Rate-limit cache unavailable` or `Lockout cache unavailable` events —
  ticket immediately; brute-force protection is failing open.
- Repeated 429s from one IP or one username lockout burst — security review.
- `Saleor webhook rejected: bad or missing signature` spike — security review.
- Backup job failure or missed retention run (`purge_expired_data`) — ticket.
