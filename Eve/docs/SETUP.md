# Installation and self-hosting guide

This guide takes a new clone from an empty machine to a working Eve instance.
Commands are run from the repository's `Eve/` directory unless stated
otherwise.

## 1. Prerequisites

Choose one setup path:

### Docker path (recommended)

- Git
- Docker Desktop or Docker Engine with Compose
- A Saleor Cloud environment if catalogue and checkout features are required

Docker Compose supplies PostgreSQL, MongoDB, Redis, nginx, and the Django app.

### Native Python path

- Git
- Python 3.13
- PostgreSQL 16+
- MongoDB 7+
- Redis 7+ (optional for basic development; required to reproduce production
  sessions, rate limits, and lockouts)
- A C compiler may be needed if a binary dependency is unavailable for the
  operating system

## 2. Clone and configure

```bash
git clone https://github.com/LexLPS/DjangoAppEve.git
cd DjangoAppEve/Eve
```

Copy the environment template:

```bash
# Linux/macOS
cp .env.example .env

# Windows PowerShell
Copy-Item .env.example .env
```

Generate a unique development secret instead of using the example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

Put that value in `DJANGO_SECRET_KEY`. The `.env` file is ignored by Git and
must never be committed.

## 3A. Run with Docker Compose

For Compose, keep the development runtime and set these values in `.env`:

```dotenv
DJANGO_ENV=dev
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8080
DB_SSLMODE=disable
```

The Compose file supplies the internal database hostnames, overrides the
runtime to `dev`, and runs nginx on port 8080. This is production-shaped in
its service layout, but it is not a production deployment: its internal
database traffic and public endpoint use local non-TLS networking.

```bash
docker compose up --build -d
docker compose exec web python manage.py migrate
docker compose exec web python manage.py ensure_indexes
docker compose exec web python manage.py createsuperuser
```

Open <http://localhost:8080/>. Inspect logs with:

```bash
docker compose logs -f web nginx
```

Stop the stack without deleting its database volumes:

```bash
docker compose down
```

Do not use `docker compose down -v` unless you deliberately want to erase the
local databases.

## 3B. Run natively

Create and activate a virtual environment:

```bash
python -m venv venv

# Linux/macOS
source venv/bin/activate

# Windows PowerShell
.\venv\Scripts\Activate.ps1
```

Install the fully locked dependency set:

```bash
python -m pip install --upgrade pip
python -m pip install -r requirements.lock
```

Create a PostgreSQL database and application user. One possible `psql` setup
is:

```sql
CREATE USER eve_user WITH PASSWORD 'choose-a-local-password';
CREATE DATABASE eve_db OWNER eve_user;
```

Start MongoDB and Redis, then make the `.env` connection values match those
services. Apply the schemas and indexes:

```bash
python manage.py migrate
python manage.py ensure_indexes
python manage.py createsuperuser
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. Development email messages, including email
verification and password-reset links, are printed in the terminal by default.

## 4. Configure your own Saleor environment

Eve does not ship with a shared shop. Each installation should own a separate
Saleor environment and credentials.

1. Sign in to Saleor Cloud and create a project/environment.
2. In Saleor Dashboard, create or select a channel. Record its slug, not its
   display name, as `SALEOR_CHANNEL`.
3. Configure the channel currency, country, shipping/payment behavior, and
   order settings appropriate for the installation.
4. Create a product type and products. Every product that Eve should sell must:
   - be assigned to the configured channel;
   - be published and available for purchase;
   - have at least one variant;
   - have a channel price for that variant;
   - have a stable unique slug.
5. Set the installation's endpoint values:

```dotenv
SALEOR_GRAPHQL_URL=https://your-environment.saleor.cloud/graphql/
SALEOR_CHANNEL=your-channel-slug
SALEOR_JWKS_URL=https://your-environment.saleor.cloud/.well-known/jwks.json
```

`SALEOR_API_TOKEN` is optional for public catalogue and checkout operations in
a standard Saleor configuration. If your Saleor permissions or additional
queries require an app token, create a least-privilege token and inject it as
an environment secret. Never place it in source control or browser-side code.

Restart Django after changing environment variables, then visit
`/shop/catalogue/`. A correctly published Saleor product should appear there.

## 5. Configure the Saleor webhook

Django must have a public HTTPS address before Saleor can deliver webhooks.
For local experiments, use a reputable HTTPS tunnel; for staging/production,
use the deployment's permanent domain.

Create an asynchronous custom webhook in Saleor Dashboard pointing to:

```text
https://YOUR-DJANGO-DOMAIN/payments/webhooks/saleor/
```

Subscribe to:

- `ORDER_FULLY_PAID`
- `ORDER_REFUNDED`
- `ORDER_FULLY_REFUNDED`
- `ORDER_CANCELLED`

Use this subscription query:

```graphql
subscription {
  event {
    __typename
    ... on OrderFullyPaid { order { id } }
    ... on OrderRefunded { order { id } }
    ... on OrderFullyRefunded { order { id } }
    ... on OrderCancelled { order { id } }
  }
}
```

Leave Saleor's deprecated webhook `secretKey` unset. Eve authenticates the
detached RS256 JWS in the `Saleor-Signature` header against Saleor's JWKS. It
uses the signed `__typename` to select the order transition and does not trust
an unsigned event header.

## 6. Test before enabling checkout

Run the isolated suite; it does not contact the real Saleor instance:

```bash
python manage.py test --settings=eve.settings.test
```

Run code-quality and security checks when developing:

```bash
ruff check .
bandit -r . -c bandit.yaml -ll
pip-audit -r requirements.lock
```

The two live Saleor tests are intentionally skipped in normal runs. With the
Saleor variables configured, run them explicitly:

```bash
# Linux/macOS
SALEOR_INTEGRATION=1 python manage.py test \
  payments.tests.SaleorIntegrationTests --settings=eve.settings.test

# Windows PowerShell
$env:SALEOR_INTEGRATION = "1"
python manage.py test payments.tests.SaleorIntegrationTests --settings=eve.settings.test
Remove-Item Env:SALEOR_INTEGRATION
```

The checkout test creates a real checkout in the configured Saleor
environment. Use staging data, not a live customer store, for initial testing.

Only after these tests pass and a signed paid/refund webhook round-trip is
verified should this be changed:

```dotenv
CHECKOUT_ENABLED=True
```

## 7. Environment variable reference

The complete safe template is `.env.example`. Important groups are:

| Group | Variables | Notes |
|---|---|---|
| Django | `DJANGO_ENV`, `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS` | Staging and production fail closed when required values are absent. |
| PostgreSQL | `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `DB_HOST`, `DB_PORT`, `DB_SSLMODE` | Production expects TLS by default. |
| MongoDB | `MONGODB_URI`, `MONGODB_DB_NAME`, `MONGODB_MAX_POOL_SIZE` | Production requires `mongodb+srv://` or `?tls=true`. |
| Redis | `REDIS_URL`, `REDIS_MAX_CONNECTIONS` | Required outside development for shared security state. |
| Saleor | `SALEOR_GRAPHQL_URL`, `SALEOR_CHANNEL`, `SALEOR_API_TOKEN`, `SALEOR_JWKS_URL` | Use HTTPS and a least-privilege token. |
| Email | `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `DEFAULT_FROM_EMAIL` | Required by production SMTP settings. |
| Proxy | `DJANGO_TRUSTED_PROXIES`, `GUNICORN_FORWARDED_ALLOW_IPS` | Trust only the actual reverse proxy. Railway currently uses `100.64.0.0/10` for the observed edge peer range. |
| Admin | `DJANGO_ADMIN_URL`, `DJANGO_ADMIN_REQUIRE_MFA`, `DJANGO_ADMIN_ALLOWED_IPS` | MFA defaults on in production. |
| Monitoring | `LOG_FORMAT`, `LOG_LEVEL`, `SENTRY_DSN`, `SENTRY_TRACES_SAMPLE_RATE` | JSON logs are recommended in production. |

Boolean values use `True` or `False`. Comma-separated variables must not be
wrapped in JSON syntax.

## 8. Administrator MFA

Production requires administrator MFA by default. After creating a
superuser, provision its first TOTP device from a trusted shell:

```bash
python manage.py provision_totp ADMIN_USERNAME
python manage.py audit_admins
```

Store recovery information securely. The admin URL should also be changed in
production, and access can be restricted with `DJANGO_ADMIN_ALLOWED_IPS`.

## 9. Deployment checklist

At minimum, a staging or production deployment must provide:

- HTTPS and a stable domain
- managed PostgreSQL with backups and TLS
- private/authenticated MongoDB with TLS
- shared Redis
- SMTP credentials
- its own Saleor environment, token, channel, and webhook
- a strong unique Django secret key
- correct allowed hosts, CSRF origins, and trusted proxy ranges
- automatic migrations before application rollout

Every deployment should run these idempotent release tasks:

```bash
python manage.py migrate --noinput
python manage.py ensure_indexes
python manage.py check --deploy --fail-level WARNING
```

After deployment, require HTTP 200 from `/healthz/ready/`. On Railway, use
`python manage.py migrate --noinput` as the pre-deploy command; run
`ensure_indexes` in the same controlled release phase or as a separate job.

Do not set `GUNICORN_FORWARDED_ALLOW_IPS=*` on an application port reachable
by untrusted networks. Set `DJANGO_TRUSTED_PROXIES` only to the actual proxy IP
or CIDR, otherwise users can spoof the IP used by rate limiting and admin
allowlists.

Read the remaining production runbooks before accepting users or payments:

- `docs/DEPLOYMENT.md`
- `docs/SECURITY_OPERATIONS.md`
- `docs/OBSERVABILITY.md`
- `docs/RELEASE.md`
- `docs/THREAT_MODEL.md`

## 10. Common problems

### Catalogue is empty

Check the GraphQL URL and channel slug. Confirm each product is published,
available for purchase, assigned to the channel, and has a priced variant.

### Cart operations fail

Check MongoDB connectivity and run `python manage.py ensure_indexes`.

### Users are rate-limited together after deployment

The reverse proxy trust settings are wrong or absent. Configure
`DJANGO_TRUSTED_PROXIES` for the real proxy network and never trust forwarded
headers from arbitrary peers.

### Webhooks return 401

Confirm the request comes from the configured Saleor environment, the JWKS URL
is correct, the webhook has no legacy shared secret, and the subscription body
matches this guide.

### Production refuses to start

This is usually an intentional fail-closed check. Review the error for a
missing secret, insecure backend URL, default database password, absent Redis,
or non-HTTPS Saleor/JWKS endpoint.

### Celery tasks do not run

Confirm the dedicated broker is reachable through `CELERY_BROKER_URL`, at
least one worker consumes the configured queues, and exactly one Beat service
is running. See `docs/BACKGROUND_JOBS.md`.
