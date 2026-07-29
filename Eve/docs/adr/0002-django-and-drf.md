# ADR-0002 — Python, Django, and Django REST Framework

**Status:** Accepted · **Date:** 2026-07

## Context

The product is a transactional commerce backend handling personal and
health-adjacent data, with both a server-rendered storefront and a REST API
for future clients. Security and correctness matter more than raw
throughput: the expected load is modest, but a mishandled order or a leaked
patient field is unacceptable.

## Decision

Python 3.13 with Django 5.2 and Django REST Framework, plus Celery for
background work.

The deciding factor was **how much security and correctness machinery comes
already built and audited**: ORM query parameterisation, CSRF protection,
password hashing with modern defaults, session management, migrations, the
`check --deploy` audit, and an admin interface. Every one of those is a
thing we would otherwise have to implement and defend ourselves.

DRF adds the API layer with serializer validation, authentication classes,
throttling, pagination, and a schema generator — again, all standard and
reviewed, rather than hand-rolled.

## Alternatives considered

**FastAPI.** Genuinely attractive: faster on paper, async-native, and
excellent OpenAPI generation. Rejected because it would have meant assembling
auth, admin, ORM/migrations, and CSRF from separate libraries, and because
the storefront needs server-rendered HTML that Django templates give for
free. Our bottleneck is upstream Saleor latency and database I/O, not the
framework's request overhead — so the performance argument does not apply to
this workload.

**Node/Express or Go.** Rejected: no compelling advantage for this workload,
and each would have meant less batteries-included security.

## Consequences

*Accepted:*
- Synchronous WSGI: concurrency comes from processes and threads, so the
  external Saleor call is a blocking call bounded by timeouts, retries, and
  a circuit breaker rather than by async I/O.
- Django's conventions (settings modules, app layout) shape the codebase.

*Gained:*
- `manage.py check --deploy` is a first-class release gate, extended with
  our own checks (`eve.W001`, `eve.W002`).
- Management commands gave us operational tooling almost free:
  `purge_user`, `export_user`, `reconcile_orders`, `sample_resources`.
- One language across web, API, jobs, and ops scripts.

## Revisit when

Sustained concurrency becomes dominated by waiting on external I/O per
request, at which point ASGI (Django async views or a separate async
service) becomes worth the migration cost.
