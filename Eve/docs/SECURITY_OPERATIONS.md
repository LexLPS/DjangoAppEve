# Security Operations

Operational controls for running Eve in production. Code-level controls
(fail-closed settings, CSP, throttling, input validation) live in the codebase;
this document covers what must be done around it.

## Monitoring

- **Health endpoint:** `GET /healthz/` returns `{"status": "ok"}` (200) or
  `{"status": "degraded"}` (503). It checks PostgreSQL and MongoDB reachability
  and never exposes failure details (those go to server logs). Point an
  uptime monitor (UptimeRobot, Pingdom, or your platform's checks) at it with
  an alert on non-200.
- **Logs:** everything goes to stdout/stderr in `{asctime} {levelname} {name}
  {message}` format — ship container/process output to your log platform.
  Watch specifically:
  - `django.security.*` — CSRF failures, `DisallowedHost` (probing)
  - `django.request` — spikes in 4xx/5xx
  - `ecommerce.views` / `core.views` — Saleor outages, health-check failures
- **Alert thresholds to start with:** any 5xx rate > 1% over 5 minutes;
  repeated 429s from a single IP (credential stuffing); healthz failing twice
  in a row.
- **Dependency audit:** run `pip-audit -r requirements.lock` at least monthly
  and on every dependency change (ideally in CI). Upgrade and re-lock when
  advisories appear.

## Backups

Two databases hold state; both need backups.

- **PostgreSQL** (users, profiles, orders, contact messages — the system of
  record):
  - Nightly `pg_dump -Fc eve_db` (or managed-provider automated backups).
  - Retention: 30 daily + 12 monthly. Encrypt at rest; store in a separate
    account/region from the primary.
  - **Test restores quarterly** — an untested backup is not a backup:
    `pg_restore --dbname=eve_restore_test <dumpfile>`, then spot-check row
    counts for `auth_user`, `accounts_profile`, `payments_order`.
- **MongoDB** (carts, product cache) — run `python manage.py ensure_indexes`
  at every deploy (idempotent; creates the unique cart index the atomic
  upserts rely on):
  - Product cache is disposable (repopulates from Saleor) — no backup needed.
  - Carts are convenience data: nightly `mongodump --db EVEDB
    --collection carts` is sufficient; losing a day of carts is acceptable,
    losing orders is not.
- **Secrets:** the production `.env` values (or platform secret store) must be
  backed up separately in a password manager / secrets vault. A database
  backup is useless if `DJANGO_SECRET_KEY` is lost — active sessions and
  password-reset tokens are invalidated when it changes.

## Redis

Redis backs the Django cache, rate-limit/lockout counters, and cached
sessions. It is **required in production** (`REDIS_URL`, `redis://` or
`rediss://` — prefer TLS for anything crossing a network boundary).

- **Authoritative data lives elsewhere:** PostgreSQL holds orders, payments,
  users, and the session source of truth (`cached_db` engine); Redis is an
  accelerator. Losing Redis loses nothing permanent.
- **Connection limits/timeouts** (set in settings): pool capped at
  `REDIS_MAX_CONNECTIONS` (default 50) per worker; 2 s connect and socket
  timeouts with retry-on-timeout. Size `maxclients` on the server above
  `workers × pool size`.
- **Eviction policy:** set `maxmemory` and `maxmemory-policy volatile-lru`.
  All app keys carry TTLs; `volatile-lru` evicts only expiring keys, so a
  full cache degrades gracefully instead of erroring. Do not use
  `allkeys-random`.
- **Failover:** use a managed HA Redis or Sentinel. The app fails safe
  without it: sessions fall back to PostgreSQL reads, and rate limiting
  fails open with loud `Rate-limit cache unavailable` error logs — alert on
  that message, because brute-force protection is reduced while it fires.

## Checkout enablement runbook

Checkout ships **disabled** (`CHECKOUT_ENABLED=False`). Enable it only after
all of the following, in order:

1. Configure `SALEOR_GRAPHQL_URL`, `SALEOR_API_TOKEN`, and channel against
   the production Saleor instance.
2. Create a Saleor webhook for order events (fully paid, payment failed,
   refunded, cancelled) pointing at `/payments/webhooks/saleor/` with a
   strong `secretKey`; set the same value as `SALEOR_WEBHOOK_SECRET`.
3. Run the integration suite against that instance and require green:
   `SALEOR_INTEGRATION=1 python manage.py test payments`
4. Set `CHECKOUT_ENABLED=True` (production refuses this without the webhook
   secret) and verify one real end-to-end order, its webhook state change to
   `paid`, and a refund round-trip before announcing availability.

## Admin protection

- Set `DJANGO_ADMIN_URL` to a non-obvious path in production (e.g.
  `manage-eve-8c1f/`). This is obscurity, not security — it cuts scanner
  noise; the real controls are below.
- **MFA is mandatory in production** (`ADMIN_REQUIRE_MFA`, default on): the
  admin login demands a TOTP token. Bootstrap: create the first device with
  `python manage.py provision_totp <username>` (prints an otpauth:// URL and
  QR to scan). A locked-out admin gets a new device the same way via shell
  access — never by disabling MFA globally.
- Admin login is rate-limited (5 attempts / 5 min / IP) in code.
- **Network restriction — pick at least one:** VPN or SSO gateway in front of
  the admin path at the reverse proxy (preferred), or the built-in allowlist
  via `DJANGO_ADMIN_ALLOWED_IPS` (comma-separated; unlisted IPs get 404).
  The allowlist checks `REMOTE_ADDR`, so the proxy must pass real client IPs.
- Admin accounts: unique per person (no shared logins), strong passwords,
  `is_superuser` only where unavoidable — content managers get scoped
  permissions via groups.
- **Quarterly access review:** run `python manage.py audit_admins` — it lists
  every staff/superuser account with last login, active state, and MFA device
  count. Remove stale privileges, deactivate departed users (same day as
  offboarding), and provision MFA for any admin flagged `NO MFA DEVICE`.
  Record the review date and outcome.

## Privacy procedures

The `Profile` model stores **health-adjacent personal data** (long-term
patient status, hospital name, room number). Treat it as sensitive under
GDPR — hosting and processors need a DPA, and data minimization applies.

- **Data inventory:** PostgreSQL: `auth_user` (username, email, password
  hash), `accounts_profile` (patient status, hospital, room), `payments_order`
  (purchase history), `core_contactmessage` (name, email, free text).
  MongoDB: `carts` (user id + items). Logs: IPs and paths, no bodies.
- **Deletion requests (right to erasure):** run
  `python manage.py purge_user <username> --yes` — deletes the user, profile,
  and orders (FK cascade) and the MongoDB cart in one step. Verify the user's
  email no longer appears in `core_contactmessage`; delete those rows manually
  if the requester asks. Complete within 30 days of the request.
- **Access/export requests:** query the tables in the inventory for the
  user's records and provide them in a readable format (JSON/CSV). Log the
  request and completion date.
- **Retention:** purge `core_contactmessage` rows older than 12 months;
  MongoDB carts untouched for 12 months can be deleted. Orders follow
  commercial/tax retention law (typically 6–10 years) — they survive account
  deletion only if law requires it; otherwise they cascade.
- **Breach response:** if personal data may have been exposed — rotate
  `DJANGO_SECRET_KEY`, all DB credentials, and the Saleor token; force
  password resets; preserve logs; assess scope. GDPR requires notifying the
  supervisory authority within **72 hours** of becoming aware, and affected
  users without undue delay when risk is high. Given patient data, err toward
  notifying.
- **Least data:** don't add new personal fields without a purpose; never log
  request bodies or query strings containing personal data.
