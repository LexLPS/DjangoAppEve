# ADR-0003 — PostgreSQL + MongoDB + Redis

**Status:** Accepted, with reservations · **Date:** 2026-07

## Context

The system stores four kinds of state with genuinely different
requirements:

| State | Requirement |
|---|---|
| Users, orders, payment state, audit log | Transactional, relational, durable — the system of record |
| Carts | Schemaless, high-churn, per-user, loss-tolerant |
| Product cache | Copy of upstream Saleor data, disposable, TTL'd |
| Rate limits, locks, session cache | Fast, shared across workers, expiring, disposable |

Running three databases in one small application invites the question of
whether this is sophistication or over-engineering. This record answers it
honestly.

## Decision

- **PostgreSQL** is authoritative for everything that must survive:
  users, `Order`, `CheckoutAttempt`, `WebhookEvent`, `PrivacyActionLog`,
  and the session source of truth.
- **MongoDB** holds carts and the product cache: documents mirroring
  Saleor's nested product shape, mutated with atomic operators
  (`$inc`, guarded `$push`, `$pull`) so concurrent requests cannot lose
  each other's writes.
- **Redis** holds the cache, rate-limit and lockout counters, cache leases,
  the JWKS cache, and the Celery broker.

The rule that keeps this coherent: **only PostgreSQL is authoritative.**
Losing Mongo costs carts and a cache; losing Redis costs throttling
precision and cached sessions. Neither can lose an order or a payment state.

## Alternatives considered

**PostgreSQL only, using JSONB for carts and the cache.** This is the
strongest alternative and would have worked. One database means one backup
story, one connection pool to size, one failure mode, and one operational
skill set. JSONB with a GIN index handles the cart shape perfectly well, and
`ON CONFLICT` gives the same atomicity.

**Redis for carts instead of Mongo.** Also viable — carts are ephemeral —
but carts would then share a failure domain with rate limiting, and Redis
is configured with eviction, so a cart could vanish under memory pressure.

## Consequences

*Accepted (the honest cost):*
- Three backup/restore procedures, three connection pools to budget, three
  sets of credentials to rotate, three health checks.
- The readiness probe and resource telemetry are correspondingly more
  complex (`docs/OBSERVABILITY.md`).
- **A reviewer could reasonably call this over-engineered for the current
  data volume, and they would not be wrong.** The justification is fit per
  workload and isolating volatile data from the system of record — not
  performance, which PostgreSQL alone would have delivered at this scale.

*Gained:*
- Cart writes never touch the transactional database, so cart churn cannot
  contend with order writes.
- The product cache can be dropped or rebuilt at will with no migration.
- Redis eviction pressure can never evict an order.

## Revisit when

Operational overhead outweighs the isolation benefit — most likely if the
team shrinks or if Mongo's failure modes cost more incidents than the
separation saves. Migrating carts to PostgreSQL JSONB is deliberately cheap:
they are reachable only through `ecommerce/services/cart_service.py`, so the
swap is one module plus its tests.
