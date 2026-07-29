# ADR-0001 — Modular monolith, not microservices

**Status:** Accepted · **Date:** 2026-07

## Context

Eve sells VR experiences to long-term hospital patients. The system has to
serve a storefront, a REST API, a catalogue backed by an external commerce
platform, carts, an idempotent checkout, and asynchronous jobs. It is built
and operated by a very small team, and the traffic ceiling is modest
(hundreds of concurrent users, not millions).

The obvious modern default is microservices — a catalogue service, a cart
service, a payment service. That default deserved an explicit decision
rather than an assumption.

## Decision

One deployable Django application, internally separated into apps with
explicit boundaries (`core`, `accounts`, `ecommerce`, `payments`, `api` —
roughly 1,000–2,000 lines each), plus a separately scalable Celery worker
process for background jobs.

Boundaries are enforced by convention rather than the network: business
logic lives in `*/services/` modules, and both the HTML views and the API
call those services. No app reaches into another app's views.

## Alternatives considered

**Microservices per domain.** Rejected: distributed transactions across a
cart service and a payment service would have to solve exactly the problem
we already solve locally with a database transaction plus a durable
`CheckoutAttempt` journal. The team is too small to operate the resulting
fleet, and the traffic does not justify independent scaling of the pieces.
It would buy independent deployability we do not currently need, and cost
network failure modes we would then have to engineer around.

**Serverless functions.** Rejected: the workload is long-lived and
connection-pool heavy (three datastores plus an upstream GraphQL API);
per-invocation cold starts and connection churn fit it badly.

## Consequences

*Accepted:*
- One release unit — a change to payments redeploys the catalogue too.
- A memory leak or crash affects the whole application, mitigated by
  worker recycling and multiple stateless pods.
- Discipline is required to keep the app boundaries meaningful, since
  nothing physically prevents a cross-import.

*Gained:*
- Checkout correctness is a local database transaction, not a saga.
- Horizontal scaling still works: pods are stateless, so scaling is
  adding replicas (see docs/DEPLOYMENT.md).
- The API could be extracted into its own service later precisely because
  the service layer already exists — the seam is drawn.

## Revisit when

One module needs a fundamentally different scaling profile or release
cadence (a likely first candidate: webhook ingestion), or the team grows
past the point where a single release train is comfortable.
