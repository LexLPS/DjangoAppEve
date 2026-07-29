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
- **Order reconciliation:** run `python manage.py reconcile_orders` daily
  (alongside `purge_expired_data`); alert whenever it reports orders that
  exist in Saleor but not locally, and investigate before running `--fix`.

## Backups

The detailed machine-verifiable procedure and signing-key overlap rotation
are in `docs/DATA_PROTECTION.md`. Run `python manage.py audit_data_protection`
as a production release gate; it validates backup/restore evidence without
printing secret values or connection details.

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

## Encryption

- **In transit:** enforced by configuration — production refuses plaintext
  backends (Postgres `sslmode=require`, Mongo `mongodb+srv`/`tls=true`,
  Saleor https, Redis `rediss://` recommended and mandatory across network
  boundaries). Client → LB is TLS with HSTS.
- **At rest:** enable storage-level encryption on all three databases
  (managed-provider default or LUKS/dm-crypt self-hosted).
- **Backups:** encrypt before leaving the host — `pg_dump | age -r <key>`
  (or gpg), same for `mongodump` archives. Store keys in the vault, not
  beside the backups.

## Recovery objectives and restore testing

- **RPO (max data loss): 24 h** with nightly dumps; reduce to ≤ 5 min for
  PostgreSQL by enabling WAL archiving / point-in-time recovery on the
  managed provider before real payment volume.
- **RTO (max downtime): 4 h** — restore both databases, redeploy the last
  known-good image, verify probes.
- **Quarterly restore test (both engines, on a scratch host):**
  1. `pg_restore --dbname=eve_restore_test <latest dump>`; verify row counts
     for `auth_user`, `accounts_profile`, `payments_order`,
     `accounts_privacyactionlog` against production ±1 day.
  2. `mongorestore --nsInclude 'EVEDB.carts' <latest archive>`; verify cart
     count and one spot-checked document.
  3. Point a staging app instance at the restored databases;
     `/healthz/ready/` must return 200 and a test login must succeed.
  4. Record date, dump used, durations (vs RTO), and any gaps. A restore
     that was never tested is not a backup.

## Credential rotation runbook

Rotate on schedule (yearly), on staff departure, and immediately on any
suspicion of exposure. Order matters:

1. `DJANGO_SECRET_KEY` — generate, deploy, rolling restart. Invalidates all
   sessions and pending reset/verification tokens (users re-login).
2. `DB_PASSWORD`, `MONGODB_URI` credentials, Redis auth — create the new
   credential, deploy, rolling restart, then revoke the old one (zero
   downtime; old credential stays valid during the roll).
3. `SALEOR_API_TOKEN` — rotate in Saleor first, deploy the new value, then
   verify one API request. Saleor rotates webhook signing keys through its
   JWKS; Eve refreshes the cached keys automatically.
4. `EMAIL_HOST_PASSWORD`, `SENTRY_DSN` — rotate at the provider, deploy.

After any rotation: confirm `/healthz/ready/`, one login, one webhook, and
record the rotation (date, secrets touched, operator).

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
2. Create a Saleor webhook for order events (fully paid, fully refunded,
   refunded, cancelled) pointing at `/payments/webhooks/saleor/`. Leave the
   deprecated `secretKey` unset so Saleor sends a detached RS256 JWS. Eve
   derives `/.well-known/jwks.json` from `SALEOR_GRAPHQL_URL`; set
   `SALEOR_JWKS_URL` only when an explicit override is required.
   Use a subscription payload that includes the signed GraphQL type name:

   ```graphql
   subscription {
     event {
       __typename
       ... on OrderFullyPaid { order { id } }
       ... on OrderRefunded { order { id } }
       ... on OrderFullyRefunded { order { id } }
       ... on OrderCancelled { order { id } }
     }
   }
   ```

   Subscribe to `ORDER_FULLY_PAID`, `ORDER_REFUNDED`,
   `ORDER_FULLY_REFUNDED`, and `ORDER_CANCELLED`. Eve intentionally derives
   the transition from the signed `__typename` body field rather than an
   unsigned request header.
3. Run the integration suite against that instance and require green:
   `SALEOR_INTEGRATION=1 python manage.py test payments`
4. Set `CHECKOUT_ENABLED=True` and verify one real end-to-end order, its webhook state change to
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
- **Data minimization:** hospital and room fields are excluded from the API
  (server-rendered profile page only), never logged, and should be left
  blank unless care delivery actually requires them. Challenge any new
  patient-related field before adding it.
- **Deletion requests (right to erasure):** run
  `python manage.py purge_user <username> --yes --performed-by <operator>` —
  deletes the user, profile, and orders (FK cascade) and the MongoDB cart in
  one step, and writes an entry to the privacy audit log
  (`accounts_privacyactionlog`). Verify the user's email no longer appears
  in `core_contactmessage`; delete those rows manually if the requester
  asks. Complete within 30 days of the request.
- **Access/export requests:** run
  `python manage.py export_user <username> --performed-by <operator>` —
  outputs all held personal data (account, profile, orders, contact
  messages, cart) as JSON and records the export in the privacy audit log.
  Deliver securely; never by plain email attachment.
- **Retention (automated):** `python manage.py purge_expired_data` deletes
  contact messages and abandoned carts past their retention windows
  (`CONTACT_MESSAGE_RETENTION_DAYS` / `ABANDONED_CART_RETENTION_DAYS`,
  default 365). **Schedule it daily** (cron or a scheduled CI job) and alert
  if a day is missed. Orders follow commercial/tax retention law (typically
  6–10 years) — they survive account deletion only if law requires it;
  otherwise they cascade.
- **Audit:** every export/deletion is recorded in
  `accounts_privacyactionlog` (action, subject, operator, timestamp — never
  the exported content). Review the log during the quarterly access review.
- **Breach response:** if personal data may have been exposed — rotate
  `DJANGO_SECRET_KEY`, all DB credentials, and the Saleor token; force
  password resets; preserve logs; assess scope. GDPR requires notifying the
  supervisory authority within **72 hours** of becoming aware, and affected
  users without undue delay when risk is high. Given patient data, err toward
  notifying.
- **Least data:** don't add new personal fields without a purpose; never log
  request bodies or query strings containing personal data.
