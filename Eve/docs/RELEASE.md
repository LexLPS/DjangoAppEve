# Release Pipeline

## Environments

| Environment | Settings | Purpose |
|---|---|---|
| development | `DJANGO_ENV=dev` | local work; console email, LocMem cache OK |
| staging | `DJANGO_ENV=staging` | production hardening, staging backends; every release lands here first |
| production | `DJANGO_ENV=prod` | real users; deploy only what staging validated |

Staging inherits **all** production fail-closed checks — it will refuse to
boot with missing secrets or non-TLS backends, exactly like production.
Each environment has its own database, Mongo, Redis, Saleor channel, and
secrets; nothing is shared.

## Secrets

Secrets live in a managed vault — GitHub Environments for the pipeline,
your platform's secret manager (AWS Secrets Manager / GCP Secret Manager /
Vault) at runtime — and are injected as environment variables. Rules:

- No secrets in the repository, images, or compose files. `.env` files are
  a development convenience only and are gitignored + dockerignored.
- Separate values per environment; staging secrets never unlock production.
- Rotation runbook: docs/SECURITY_OPERATIONS.md.

## Pipeline (.github/workflows)

- `ci.yml` runs on every push/PR: tests, `check --deploy`, pip-audit,
  ruff+bandit, gitleaks. Required checks block merge.
- `release.yml` (manual dispatch, per environment): locked dependency
  install (`requirements.lock`), full test suite, image build (collectstatic
  happens inside the build), then deploy gated by the GitHub Environment
  (add required reviewers on the production environment for a two-person
  rule). Release tasks per deploy: `migrate` → `ensure_indexes` →
  rolling rollout.

## Rollouts and rollback

- **Rolling or blue-green only.** New pods must return 200 from
  `/healthz/ready/` before receiving traffic; old pods drain afterwards.
  Never stop-the-world.
- **Migrations are backward-compatible (expand/contract):** additive changes
  ship first (new nullable columns, new tables); code that stops using old
  columns ships next; destructive drops ship at least one release later,
  once no running version references them. Never rename in place.
- **Rollback = redeploy the previous image tag.** Because migrations are
  expand/contract, the previous release always runs against the current
  schema — no database rollback needed or wanted. Practice a rollback on
  staging at least once per quarter.

## Before accepting real users or payments (go-live gate)

1. **Load test** against staging with production-shaped data: browse and
   checkout scenarios (k6 or locust), ramp to 2× expected peak. Pass =
   SLOs held (docs/OBSERVABILITY.md) and no resource exhaustion (DB
   connections, Redis pool, worker saturation).
2. **Final security review:** re-run the full CI security gates; verify the
   go-live checklist — checkout enablement runbook completed (integration
   tests green against production Saleor, webhook secret set), admin MFA
   devices provisioned, `audit_admins` clean, backups + restore test done
   within the last quarter, alerting wired to a paged channel, secrets
   rotated out of any pre-launch values, and a penetration test or external
   review for the payment flow.
3. Sign-off recorded (who, when, what was verified) before
   `CHECKOUT_ENABLED=True` reaches production.
