# ADR-0004 — Saleor owns the catalogue and payment

**Status:** Accepted · **Date:** 2026-07

## Context

The product needs a catalogue, pricing, tax handling, checkout, and payment
capture. Building that ourselves would mean owning money handling and — if
card data ever touched our servers — PCI DSS scope, for a student-scale team
serving a healthcare-adjacent audience.

## Decision

Saleor Cloud is the commerce backbone. Eve owns identity, the storefront,
the API, carts, and the local order journal; Saleor owns product data,
price calculation, checkout completion, and payment.

Three rules follow, and they shape a lot of the codebase:

1. **Prices are never trusted from the client or the cart.** Cart amounts
   are display-only; Saleor recalculates every total at `checkoutCreate`.
2. **Saleor is treated as untrusted input.** Product documents are
   validated (`is_valid_product`) and sanitised (http(s)-only URLs,
   `striptags`) before being cached, rendered, or serialised.
3. **Saleor is treated as unreliable.** Calls run behind timeouts, bounded
   retries with jitter (reads only — mutations never auto-retry), a shared
   circuit breaker, and a TTL cache that can serve stale data during an
   outage.

## Alternatives considered

**Build catalogue, pricing, and payment in-house** (e.g. Stripe directly
plus our own product model). Rejected: it puts us in the payment path and
multiplies the compliance and correctness burden. Stripe alone would still
leave catalogue, pricing, and tax to build.

**Another headless platform** (Medusa, Commerce Layer). Comparable; Saleor's
GraphQL API and hosted offering fit the timeline. Not a load-bearing choice.

## Consequences

*Accepted:*
- A hard dependency on an external system for core function: when Saleor is
  down, the catalogue degrades to cached data and checkout is unavailable.
- Non-idempotent mutations across a network boundary forced the durable
  `CheckoutAttempt` journal, per-user leases, and the `reconcile_orders`
  job — real complexity that a local-only checkout would not need.
- Order state arrives asynchronously via webhooks, which must therefore be
  signature-verified (RS256/JWKS), deduplicated, and transition-guarded.
- Two systems can disagree about reality; reconciliation is a scheduled job,
  not an assumption.

*Gained:*
- No card data ever touches Eve, so PCI scope stays with Saleor.
- Pricing, tax, and inventory correctness are not ours to get wrong.

## Revisit when

The external dependency's availability becomes the dominant source of user
harm, or commercial terms change. Note that the coupling is contained:
Saleor is reached only through `ecommerce/services/saleor_client.py` and
`payments/services/saleor_checkout.py`.
