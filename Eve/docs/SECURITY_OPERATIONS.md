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
- **MongoDB** (carts, product cache):
  - Product cache is disposable (repopulates from Saleor) — no backup needed.
  - Carts are convenience data: nightly `mongodump --db EVEDB
    --collection carts` is sufficient; losing a day of carts is acceptable,
    losing orders is not.
- **Secrets:** the production `.env` values (or platform secret store) must be
  backed up separately in a password manager / secrets vault. A database
  backup is useless if `DJANGO_SECRET_KEY` is lost — active sessions and
  password-reset tokens are invalidated when it changes.

## Admin protection

- Set `DJANGO_ADMIN_URL` to a non-obvious path in production (e.g.
  `manage-eve-8c1f/`). This is obscurity, not security — it cuts scanner
  noise; the real controls are below.
- Admin login is rate-limited (5 attempts / 5 min / IP) in code.
- Restrict the admin path by IP allowlist or VPN at the reverse proxy where
  possible.
- Admin accounts: unique per person (no shared logins), strong passwords,
  `is_superuser` only where unavoidable — content managers get scoped
  permissions via groups. Review the account list quarterly; disable accounts
  on offboarding the same day.

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
