# Deployment resilience

## Railway environment separation

Maintain two **persistent, isolated** Railway environments named exactly
`staging` and `production`. Do not share PostgreSQL, MongoDB, cache Redis,
broker Redis, Saleor environments/channels, domains, credentials, or backup
evidence. Set `DJANGO_ENV=prod` in both deployed environments. Railway provides
`RAILWAY_ENVIRONMENT_NAME`, which must be `staging` or `production`; Eve maps
`DJANGO_ENV=prod` to the expected Railway name `production` and refuses crossed
configuration. Set `DEPLOYMENT_ENVIRONMENT` explicitly only outside Railway.

Create staging by duplicating production only as a starting topology. Before
approving its staged services, replace every referenced backend and secret
with staging-specific values. Use `staging` for the staging environment and
`main` only for approved production releases. Disable production GitHub
autodeploy unless required GitHub check suites are configured to block it.

Assign service config-file paths in Railway:

| Service | Config path | Replicas |
|---|---|---|
| web | `/deploy/railway.web.json` | at least 2 in production |
| worker | `/deploy/railway.worker.json` | at least 1; scale by queue age |
| beat | `/deploy/railway.beat.json` | exactly 1 |

The web health check, 30-second deployment overlap, and 30-second Gunicorn
drain provide zero-downtime replacement. Do not attach a web health path to
worker or Beat. Railway variables must reference backends inside the same
environment.

## Controlled release order

Deploy from a full immutable Git commit SHA. The web pre-deploy container runs:

```bash
python manage.py release_preflight --migrate
```

It runs fail-closed production checks, verifies recovery evidence in
production, applies backward-compatible Django migrations once, ensures Mongo
indexes, and confirms no migrations remain. Then deploy worker and Beat; their
`--schema-only` preflight refuses to start until the web migration succeeded.

Release staging first, run the acceptance smoke set, then promote the same Git
SHA to production. Do not rebuild from an uncommitted workspace. Dependencies
come only from the fully pinned `requirements.lock`; CI rejects mutable entries
and records the image identity for each SHA.

## Post-deployment verification

Railway reports deployment status to GitHub. On success,
`post-deploy.yml` checks the deployed revision and requires three consecutive
successful liveness/readiness rounds over HTTPS. Run it manually when needed:

```bash
python deploy/verify_deployment.py --base-url https://staging.example.com
```

Do not promote while probes, checkout reconciliation, webhook backlog, worker
queue age, or error alerts are unhealthy.

## Rollback

1. Stop promotion and record the failing deployment ID and commit.
2. If data integrity is in doubt, set `CHECKOUT_ENABLED=False` first.
3. In Railway, select the previously successful deployment and choose
   **Rollback**. Railway restores that image and its deployment variables.
4. Never reverse a production migration. The expand/contract policy keeps the
   prior image compatible with the current schema.
5. Verify three healthy probe rounds, login, catalogue, queue processing, and
   one signed staging webhook.
6. Reconcile orders and uncertain checkout attempts before re-enabling checkout.
7. Record timeline, affected requests/orders, recovery action, and follow-up.

Practice this in staging quarterly. Railway retains rollback images for a
plan-dependent window, so also retain source SHA and artifact identity outside
that window.

## Failure exercises

Run quarterly in staging, one dependency at a time, with checkout disabled
unless the exercise explicitly requires it:

| Failure | Expected behavior | Recovery evidence |
|---|---|---|
| Cache Redis unavailable | readiness fails; checkout coordination fails safely; alert fires | restore Redis, readiness returns, rate limits work |
| MongoDB unavailable | readiness fails; cached catalogue can degrade; no order loss | restore Mongo, run `ensure_indexes` |
| Saleor unavailable | circuit opens; catalogue uses stale cache; checkout reports unavailable | circuit-close event after recovery |
| Broker unavailable | signed webhook receives 202 after durable PostgreSQL insert | restore broker; minute recovery publishes pending row |
| Worker stopped | webhook inbox and queue grow without losing events | restart worker; backlog drains exactly once |

Automated unit tests cover these contracts. The live exercise proves provider
networking, alert routing, restart behavior, and operational response.
