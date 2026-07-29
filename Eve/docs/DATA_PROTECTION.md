# Data protection and secrets

## Ownership and recovery objectives

PostgreSQL is authoritative for identities, sessions, orders, checkout
attempts, webhook inbox rows, and privacy audit records. MongoDB carts are
recoverable convenience state; the product catalogue cache is disposable.
Redis cache is disposable, while the dedicated Celery Redis broker must be
persistent and use `noeviction`.

- PostgreSQL target: provider point-in-time recovery with **RPO <= 5 minutes**.
- MongoDB carts target: nightly encrypted backup with **RPO <= 24 hours**.
- Whole-service target: **RTO <= 4 hours**.
- Backups: encrypted, immutable where supported, and stored in an account or
  region isolated from the primary service.

## Machine-verifiable release gate

Trusted backup automation updates these Railway variables after it verifies a
provider backup, never before it starts one:

```text
POSTGRES_BACKUP_LAST_SUCCESS_AT=2026-07-29T02:00:00Z
MONGODB_BACKUP_LAST_SUCCESS_AT=2026-07-29T02:30:00Z
RESTORE_TEST_LAST_SUCCESS_AT=2026-07-01T10:00:00Z
BACKUP_ENCRYPTION_CONFIRMED=True
BACKUP_OFFSITE_CONFIRMED=True
```

Run this before a production release:

```bash
python manage.py audit_data_protection
```

It fails closed when evidence is missing, malformed, future-dated, older than
`BACKUP_MAX_AGE_HOURS` (36 by default), or when the last restore drill is
older than `RESTORE_TEST_MAX_AGE_DAYS` (100 by default). It never prints
timestamps, endpoints, credentials, or secret values. A timestamp is an
attestation, not the backup itself; restrict variable write access to the
backup automation identity and production operators.

## Restore drill

At least quarterly, restore into isolated scratch services, run migrations
only after confirming the restored schema version, execute
`ensure_indexes`, verify representative row/document counts, start a staging
app against the restored stores, and test readiness, login, order history,
and one signed webhook. Record recovery duration, backup identifiers, the
operator, and discrepancies. Destroy scratch data securely after sign-off.

## Secret storage and rotation

Railway variables are runtime secrets; GitHub Environments hold release-only
secrets. Use unique credentials per environment and per backend, grant the
minimum provider role, prohibit secrets in logs/tickets/chat, and enable audit
logs plus MFA for every human with production-secret access.

For a zero-downtime Django signing-key rotation:

1. Generate a new random key of at least 64 bytes and make it
   `DJANGO_SECRET_KEY`.
2. Put the previous key temporarily in `DJANGO_SECRET_KEY_FALLBACKS`.
3. Rolling-deploy, verify login, password reset, and signed links.
4. After the maximum session/token lifetime, remove the fallback and deploy
   again. Never include the active key in the fallback list.

Database, MongoDB, Redis, SMTP, Sentry, and Saleor credentials use overlap
rotation: create new, deploy new, verify, then revoke old. Treat any exposure
as an incident: rotate immediately, review provider audit logs, invalidate
affected sessions/tokens, preserve evidence, and follow the breach process in
`SECURITY_OPERATIONS.md`.
