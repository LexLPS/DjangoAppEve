# Architecture Decision Records

Short records of the decisions that shaped this system: the context, the
choice, the alternatives, and the consequences we accepted. They exist so
the reasoning survives after the commit messages are forgotten — and so a
decision can be revisited deliberately rather than by accident.

| # | Decision | Status |
|---|---|---|
| [0001](0001-modular-monolith.md) | Modular monolith, not microservices | Accepted |
| [0002](0002-django-and-drf.md) | Python, Django, and Django REST Framework | Accepted |
| [0003](0003-three-datastores.md) | PostgreSQL + MongoDB + Redis | Accepted, with reservations |
| [0004](0004-saleor-as-commerce-backbone.md) | Saleor owns catalogue and payment | Accepted |

Format: context → decision → alternatives → consequences. One page each.
