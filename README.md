# Eve

Eve is a Django web application for browsing and purchasing virtual-reality
experiences. It combines Django accounts and orders with a Saleor catalogue,
MongoDB-backed carts, Redis-backed sessions and rate limits, and signed Saleor
order webhooks.

## What the application does

- Registers users, verifies email addresses, and supports login, logout,
  password reset, profiles, and privacy export/deletion workflows.
- Reads products and authoritative prices from Saleor over GraphQL.
- Caches catalogue data and stores user carts in MongoDB.
- Creates Saleor checkouts using server-side product and price data.
- Stores local order history in PostgreSQL.
- Processes paid, refunded, fully-refunded, and cancelled order events through
  RS256-signed Saleor webhooks.
- Protects authentication with shared rate limits and account lockouts.
- Supports administrator TOTP MFA, configurable admin paths, and an optional
  administrator IP allowlist.
- Provides structured request logging, Sentry integration, and liveness and
  readiness endpoints.

Checkout is disabled by default. This is intentional: every installation must
connect and test its own Saleor environment before accepting orders.

## Architecture

```text
Browser
  |
  v
reverse proxy / Railway edge
  |
  v
Django + Gunicorn
  |-- PostgreSQL: users, profiles, contact messages, orders, sessions
  |-- MongoDB: carts and product cache
  |-- Redis: cache, sessions, rate limits, account lockouts
  `-- Saleor Cloud: catalogue, checkout, order events
```

The application is stateless at the web-process layer and can be scaled across
multiple workers or instances when all three data services are shared.

## Technology

- Python 3.13 and Django 5.2
- Django REST Framework
- PostgreSQL, MongoDB, and Redis
- Saleor GraphQL
- Gunicorn and nginx/Railway
- Docker Compose for a production-shaped local stack
- GitHub Actions with tests, Ruff, Bandit, pip-audit, and Gitleaks

## Start here

The complete clone-to-running instructions are in
[Eve/docs/SETUP.md](Eve/docs/SETUP.md). They cover:

1. Native and Docker-based local setup
2. Every important environment variable
3. PostgreSQL, MongoDB, and Redis initialization
4. Creating and configuring a Saleor Cloud environment
5. Product, channel, checkout, and webhook configuration
6. Running unit and live Saleor integration tests
7. Deployment and production security requirements

For the quickest local start:

```bash
git clone https://github.com/LexLPS/DjangoAppEve.git
cd DjangoAppEve/Eve
cp .env.example .env
# Edit .env before continuing.
docker compose up --build
```

Then open <http://localhost:8080/>.

## Repository layout

```text
Eve/
|-- accounts/       Authentication, profiles, MFA and privacy commands
|-- core/           Landing/contact pages, middleware, health and throttling
|-- ecommerce/      Saleor client, catalogue cache and MongoDB cart
|-- payments/       Checkout, orders, reconciliation and signed webhooks
|-- eve/settings/   Development, test, staging and production settings
|-- deploy/         nginx configuration
|-- docs/           Setup, deployment, operations, release and threat model
|-- Dockerfile
|-- docker-compose.yml
`-- manage.py
```

## Useful commands

Run these from the `Eve/` directory with the virtual environment activated:

```bash
python manage.py migrate
python manage.py ensure_indexes
python manage.py createsuperuser
python manage.py runserver
python manage.py test --settings=eve.settings.test
```

Health endpoints:

- `/healthz/live/` confirms the Django process is running.
- `/healthz/ready/` checks PostgreSQL, MongoDB, Redis/cache, and Saleor circuit
  state.
- `/healthz/` is a compatibility alias for readiness.

## Further documentation

- [Installation and Saleor setup](Eve/docs/SETUP.md)
- [Deployment architecture](Eve/docs/DEPLOYMENT.md)
- [Security operations](Eve/docs/SECURITY_OPERATIONS.md)
- [Observability and alerts](Eve/docs/OBSERVABILITY.md)
- [Release process](Eve/docs/RELEASE.md)
- [Threat model](Eve/docs/THREAT_MODEL.md)

## License

This project is proprietary. Contact the Eve team before redistributing or
using it commercially.
