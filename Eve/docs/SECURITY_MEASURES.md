# Implemented cybersecurity measures

## Purpose and scope

This document gives an overview of the cybersecurity
measures implemented in Eve. It covers the Django application, the versioned
REST API (`/api/v1/`), its Saleor integration, PostgreSQL, MongoDB, Redis,
Celery workers, deployment controls, and the supporting development process.

**Last reviewed:** 2026-07-31 against commit `31d9a25` (branch
`capacity-analysis`). If the assessed commit in
[THREAT_MODEL.md](THREAT_MODEL.md) is newer than this one, treat this
document as potentially incomplete and re-review it.

This is an implementation summary, not a claim that the system has no
remaining risk. The complete risk analysis and accepted risks are documented
in [THREAT_MODEL.md](THREAT_MODEL.md).

## Security overview

Eve follows four main principles:

1. **Fail securely.** Production refuses to start when required security
   configuration is missing or inconsistent.
2. **Trust as little client input as possible.** Identity, ownership, prices,
   order state and webhook events are validated on the server.
3. **Use layered controls.** Authentication, authorization, validation,
   cryptographic verification, rate limiting and monitoring reinforce one
   another.
4. **Keep security verifiable.** Automated tests and CI security checks are
   used to prevent regressions.

## 1. Authentication and account security

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Argon2id password hashing with in-place upgrade | Password disclosure and weak passwords | Passwords are hashed with Argon2id, the OWASP-preferred memory-hard hasher. Existing PBKDF2 hashes continue to verify and are transparently upgraded on the owner's next successful login. Configured password validators reject weak passwords. See `PASSWORD_HASHERS` in `eve/settings/base.py`. |
| Login rate limiting | Automated password guessing | Login requests are limited by client IP using shared Redis state, so the protection works across multiple web instances. See `accounts/views.py` and `core/throttling.py`. |
| Account lockout | Repeated targeted password guessing | Repeated failures against the same username cause a temporary lockout. The account owner is notified once per lockout window without revealing whether unknown accounts exist. |
| Administrator TOTP MFA | Stolen administrator passwords | Production administrators can be required to provide a time-based one-time password. See the admin MFA middleware and production settings. |
| Secure session handling | Session theft and fixation | Sessions are server-side, cookies are `HttpOnly`, `Secure` in production and `SameSite=Lax`; session identifiers rotate at login and are invalidated at logout. |
| POST-only logout with CSRF protection | Forced logout and cross-site requests | Logout changes state only through a CSRF-protected POST request. |
| Email verification | Orders associated with unverified addresses | Checkout refuses to place an order until the account email address has been verified. |
| Case-insensitive unique email addresses | Duplicate identities and account confusion | Registration validation and a database constraint enforce one account per normalized email address. |

## 2. REST API authentication and token security

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Short-lived signed access tokens | Long-lived credential theft and database disclosure | Access tokens are stateless 15-minute tokens produced with Django's `TimestampSigner`, carrying only the user id and a version counter. Nothing is stored server-side, so a database disclosure yields no usable access credential. See `api/tokens.py`. |
| Hashed, single-use refresh tokens | Refresh-credential disclosure and silent session extension | Refresh tokens are stored as SHA-256 digests of 256-bit random values and rotate on every use: each refresh retires the presented token and issues a new one. |
| Refresh-token reuse detection | Undetected token theft | Presenting an already-rotated refresh token is treated as evidence of capture: the entire token family is revoked and a security event is logged. |
| Version-based mass revocation | Inability to revoke stateless tokens | Bumping `token_version` on the user's profile immediately invalidates every outstanding access token for that account. |
| MFA-enforced token issuance | MFA bypass through the API | An account with a confirmed TOTP device cannot obtain an API token with a password alone; issuance requires a current one-time code. See the token view in `api/v1/views.py`. |
| Shared lockout across login surfaces | Credential stuffing against the weaker endpoint | The HTML login and the API token endpoint use one lockout implementation (`accounts/services/lockout.py`), so failures against either surface lock the account and notify the owner once per window. |
| Issuance preconditions and throttles | Automated token-guessing and abuse | Token issuance requires a verified email address and is limited by both a per-IP limit and a scoped DRF throttle (`token` scope). |
| Scoped API throttling with fail-open logging | API abuse, and silent loss of protection | DRF throttles apply per scope (for example `token`, `checkout`). A cache outage fails open to preserve availability but logs a `throttle_fail_open` event so the loss of protection is visible. See `api/throttling.py`. |
| User-scoped idempotency keys | Cross-account key collision and key squatting | Client-supplied idempotency keys are hashed together with the user id before any lookup, so one account can never receive or block another account's order. See `scoped_idempotency_key` in `payments/services/checkout.py`. |
| Uniform authentication failure messages | Username and credential enumeration | Token issuance returns the same error regardless of which part of the credentials was wrong, revealing nothing about whether the account exists. See `api/v1/views.py`. |
| Code-generated OpenAPI schema and CSP-safe docs | Contract drift and script-injection through documentation pages | The `/api/v1/schema/` document is generated from the code (drf-spectacular), and a test fails the build on any generation warning. Swagger UI and ReDoc serve all assets from the application origin under the strict CSP — no CDN and no inline scripts, with a regression test asserting the docs HTML contains none. |

## 3. Authorization and privacy

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Ownership checks on user resources | Insecure direct object references | Profiles, carts and order history are selected through the authenticated user rather than trusting a user ID supplied by the browser. Tests confirm that one user cannot read another user's records. |
| Staff-only administrative access | Unauthorized privileged operations | Django's staff permissions protect the administration interface; the path is configurable and may additionally be restricted by an IP allowlist. |
| Audited privacy operations | Undetected access to personal information | Export, deletion and other privileged privacy actions are recorded in `PrivacyActionLog`. |
| Data export and deletion workflows | Failure to support data-subject rights | Management workflows allow an operator to export or delete a user's stored application data in a controlled manner. |
| Minimal payment-data handling | Exposure of card details | Eve does not store card numbers. Payment processing and authoritative payment state belong to the configured Saleor payment integration. |

## 4. Input validation and injection protection

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Django forms and serializers | Invalid or malicious input | User input is validated for type, format, length and allowed values before it reaches business logic. |
| ORM-based SQL access | SQL injection | Application database queries use Django's ORM and parameterized database operations rather than constructing SQL from user input. |
| Fixed MongoDB query structure | NoSQL injection | MongoDB field names and operators are defined by the application; user-controlled values are inserted only as values. |
| Template autoescaping and upstream sanitization | Cross-site scripting | Django autoescaping is retained. Saleor text is validated and HTML is stripped before caching or rendering. External URLs are limited to HTTP(S). |
| Quantity validation at multiple layers | Cart and checkout manipulation | Quantities are bounded in forms, views and checkout construction. Invalid or excessive quantities are rejected. |
| Bounded cart size | Storage abuse and permanent cart breakage | Carts are capped at 50 distinct products (surfaced as `409 cart_full`), preventing a client from growing a cart document toward MongoDB's 16 MB document limit, past which every write to it would fail permanently. |
| Request-size limits | Memory exhaustion and oversized webhook requests | The edge and application reject bodies above documented limits; oversized Saleor webhook tests expect HTTP 413. |

## 5. Checkout and order integrity

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Server-authoritative products and prices | Price or product manipulation in the browser | Checkout rebuilds lines from server-side Saleor data. Prices supplied by the browser or cart are never treated as authoritative. |
| Checkout feature flag | Accidental charging before integration is ready | `CHECKOUT_ENABLED` defaults to false and production requires complete Saleor configuration before checkout can be enabled. |
| Idempotency keys and durable attempts | Duplicate orders and ambiguous retries | A `CheckoutAttempt` is stored before the upstream mutation. Repeated submissions reuse the same idempotency identity, and uncertain attempts are reconciled instead of blindly retried. |
| Explicit order state machine | Invalid payment-state transitions | Only allow-listed transitions such as pending to paid, refunded or cancelled are accepted. Out-of-order transitions are rejected and logged. |
| Transactional updates and row locking | Concurrent order-update races | Webhook processing uses database transactions and row-level locking before changing order state. |
| Reconciliation process | Missing local orders after partial failures | The reconciliation command compares Saleor orders with local records and can recover matched missing orders under operator control. |

## 6. Webhook authenticity and replay resistance

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| RS256 signature verification | Forged or altered Saleor webhooks | Detached JWS signatures are verified with Saleor's public JWKS. Unsigned or invalid messages receive HTTP 401. See `payments/services/saleor_webhooks.py`. |
| HTTPS-only JWKS source | Key substitution through insecure transport | Production accepts only an HTTPS JWKS endpoint. Redirects are disabled and keys are cached with a controlled refresh. |
| Event type taken from signed content | Header/event confusion | The application derives the event type from the verified payload rather than trusting an unsigned request header. |
| Durable receipt before asynchronous processing | Lost events during broker failure | A valid event is committed to PostgreSQL before it is sent to Celery. Recovery jobs requeue durable pending events after broker failure. |
| Event uniqueness and idempotent processing | Duplicate and replayed delivery | Event identifiers are unique and already-processed events do not repeat a state change. Tests cover duplicate and out-of-order events. |

## 7. Cryptography, transport and key management

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| TLS for public traffic and data services | Network interception | Production enforces HTTPS, HSTS and secure cookies. PostgreSQL, MongoDB, Redis, Saleor and JWKS connections require encrypted production endpoints. |
| Environment-managed secrets | Secrets committed to source control | Django signing keys, database credentials, Saleor tokens and service credentials are supplied at deployment time. `.env` is for local development and is excluded from Git. |
| Signing-key rotation support | Emergency key replacement causing mass logout | Django secret-key fallbacks permit controlled signing-key rotation; production validates key length and prevents reuse of the active key as a fallback. |
| Public-key webhook validation | Shared webhook-secret exposure | Saleor webhook authenticity uses asymmetric RS256 signatures, so Eve stores public verification keys rather than a shared webhook signing secret. |
| Secret scanning | Accidental credential commits | Gitleaks runs in CI, and production documentation defines credential-rotation procedures. |

## 8. Browser and HTTP protections

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| CSRF tokens on state-changing requests | Cross-site request forgery | Django CSRF middleware protects forms and authenticated state changes. Regression tests reject requests without a valid token. |
| Content Security Policy | Script injection | CSP restricts scripts to the application origin and does not permit unsafe inline scripts. |
| Clickjacking protection | UI redressing | `X-Frame-Options: DENY` and CSP `frame-ancestors 'none'` prevent the application from being embedded in another site. |
| Additional security headers | MIME confusion and information leakage | Production sends `X-Content-Type-Options`, a restrictive referrer policy and a Permissions Policy. |
| Trusted-proxy validation | Spoofed client IP addresses | Client IP headers are honored only from configured proxy networks. Deployment checks fail when the production proxy configuration is missing. |
| Server-Timing disabled in production | Timing analysis against authentication paths | The `Server-Timing` header, which would hand any client network-noise-free internal timings, is gated on `SERVER_TIMING_ENABLED`: off in production, explicitly on in staging where the load test needs the breakdown. The same measurements remain available in the structured logs. |

## 9. Data-store and cache security

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| PostgreSQL as authoritative storage | Loss of orders or sessions through cache failure | Orders, users and sessions remain authoritative in PostgreSQL; Redis is not the sole source of critical state. |
| Safe Redis serialization | Code execution after cache compromise | Cache values use a JSON-based serializer rather than Python pickle. A compromised cache can disrupt state but cannot inject serialized Python objects. |
| Private, authenticated Redis services | Unauthorized cache and broker access | Production Redis endpoints require credentials and are intended to use private networking and TLS where supported. Cache and Celery broker responsibilities are separated. |
| Least-privilege MongoDB monitoring | Unnecessary Atlas cluster privileges | Operational telemetry uses database-scoped `dbStats`; the application user does not require the cluster-wide `serverStatus` privilege. |
| Idempotent MongoDB indexes | Duplicate carts and inefficient lookups | Controlled release steps ensure unique and lookup indexes for carts and cached products. |

## 10. Availability and scalability protections

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Stateless web processes | Single-instance dependency | Session and application state are stored in shared services, allowing multiple web replicas. |
| Timeouts, retries and circuit breaking | Cascading Saleor outages | Saleor requests have bounded timeouts, retry/backoff rules and a circuit breaker. Cached catalogue data supports degraded reads. |
| Cache-stampede locks | Sudden duplicate upstream traffic | Distributed locks ensure one process refreshes expensive catalogue data while other processes continue using cached data. |
| Separate Celery queues | One workload blocking all background work | Webhooks, orders, email, catalogue and maintenance tasks are routed separately and can be scaled independently. |
| Health endpoints | Traffic reaching an unhealthy deployment | Liveness reports process availability; readiness verifies PostgreSQL, MongoDB and Redis before a deployment receives traffic. |
| Controlled migrations and zero-downtime checks | Partially upgraded production state | Railway pre-deploy commands run checks and migrations before activation. Health verification, overlap and draining reduce release interruption. |

## 11. Logging, monitoring and incident response

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Structured logs with request IDs | Incidents that cannot be reconstructed | Requests, security events, tasks, Saleor calls and state transitions use structured logs and correlation identifiers. |
| Sensitive-data filtering | Credentials or personal data leaking into logs | Logging and Sentry configuration redact sensitive fields and avoid including upstream response bodies in exceptions. |
| Operational alerts | Silent degradation | Documented alerts cover elevated errors, queue backlog, webhook delay, circuit-breaker state, backend failures and security events. |
| Resource and queue telemetry | Capacity exhaustion | PostgreSQL, Redis, MongoDB database, Celery queue and checkout-recovery measurements are sampled without requiring elevated database privileges. |
| Backup and restoration checks | Permanent data loss | Production gates require recent backup evidence, encryption and off-site confirmation. Restoration exercises are part of production acceptance. |
| Credential-rotation procedures | Prolonged exposure after a leaked secret | The operations documentation defines rotation and verification steps for Django, PostgreSQL, MongoDB, Redis and Saleor credentials. |

## 12. Secure development and deployment controls

| Implemented measure | What it protects against | Implementation and evidence |
|---|---|---|
| Automated test suite | Security regressions | Tests cover authentication, authorization, CSRF, XSS handling, webhook signatures, idempotency, state transitions, dependency failures and deployment gates. |
| Ruff and Bandit | Common code defects and insecure Python patterns | Both scanners run as required CI checks. |
| Dependency locking and `pip-audit` | Unreviewed or vulnerable dependencies | The complete dependency closure is version-pinned. CI reproduces the container's `--no-deps` install, runs `pip check`, and audits known vulnerabilities. |
| Immutable release identity | Untraceable deployments | Releases are associated with a full Git commit SHA, and production rejects an invalid release identifier. |
| Fail-closed production settings | Unsafe defaults reaching production | Production validates hosts, trusted origins, TLS endpoints, proxy configuration, environment identity and required secrets during startup. |
| Required CI checks | Untested changes reaching the deployment branch | Tests, deployment checks, static analysis, dependency audit and secret scanning run on pull requests before merge. |

## 13. Verification summary

The controls above are verified through a combination of:

- automated unit and integration tests;
- Django's production deployment checks;
- Ruff, Bandit, Gitleaks and `pip-audit` in CI;
- health and post-deployment verification;
- load-test evidence and failure-scenario exercises;
- reconciliation, backup restoration and credential-rotation runbooks; and
- manual production-acceptance testing for checkout, refund, cancellation and
  signed webhook processing.

## 14. Known limitations and remaining work

The most important remaining items are:

- production payment capture must remain disabled until the selected Saleor
  payment application is configured and a real low-value checkout is accepted;
- the readiness endpoint should be restricted at the edge if the platform does
  not require public access;
- the container base image should be pinned by digest and scanned in CI;
- external provider configuration, backup restoration and credential rotation
  still require periodic verification;
- mixed-currency cart totals are computed incorrectly but are unreachable with
  the current single Saleor channel, and cannot misprice an order because
  checkout recalculates all amounts upstream — tracked as an open item in the
  threat model rather than fixed.

## Related documentation

- [Threat model](THREAT_MODEL.md)
- [Security operations](SECURITY_OPERATIONS.md)
- [Data protection](DATA_PROTECTION.md)
- [Deployment resilience](DEPLOYMENT_RESILIENCE.md)
- [Production acceptance](PRODUCTION_ACCEPTANCE.md)
- [Setup guide](SETUP.md)
