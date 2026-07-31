# Installation and self-hosting guide

This guide takes a new clone from an empty machine to a working Eve instance.
Commands are run from the repository's `Eve/` directory unless stated
otherwise.

## 1. Which path do you need?

The three paths differ only in how much you install. Start at the top and
move down as you need more.

| Path | Installs | Gets you |
|---|---|---|
| **A. Evaluation** | Python only | The app running, the test suite passing, the API and its docs. Demo catalogue, no real shop |
| **B. Full local** | + PostgreSQL, MongoDB, Redis | Real carts, sessions shared across workers, background jobs |
| **C. Docker** | Docker only | Path B's services in containers, production-shaped behind nginx |

All paths need **Python 3.13** and **Git**. Commands run from the
repository's `Eve/` directory unless stated otherwise.

## 2. Clone and configure (all paths)

```bash
git clone https://github.com/LexLPS/DjangoAppEve.git
```

```bash
cd DjangoAppEve/Eve
```

Create the virtual environment:

```bash
python -m venv venv
```

Activate it — `source venv/bin/activate` on Linux/macOS, or
`.\venv\Scripts\Activate.ps1` in Windows PowerShell. Then install the
locked dependency set:

```bash
python -m pip install -r requirements.lock
```

Copy the environment template:

```bash
cp .env.example .env
```

On Windows PowerShell use `Copy-Item .env.example .env`.

**`.env.example` is deliberately zero-dependency as shipped**: SQLite, an
in-process cache, and no Saleor. It runs as-is. Generate your own
development secret and paste it into `DJANGO_SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(64))"
```

`.env` is gitignored and must never be committed.

## 3A. Path A — evaluation, no services required

```bash
python manage.py migrate
```

```bash
python manage.py runserver
```

Open <http://127.0.0.1:8000/>. What works without any service installed:

- the storefront, with a small built-in demo catalogue;
- registration, login, and the profile pages;
- the REST API and its documentation at `/api/v1/docs/`;
- the whole test suite:

```bash
python manage.py test --settings=eve.settings.test
```

The suite is **hermetic** — it never contacts PostgreSQL, MongoDB, Redis,
Saleor or SMTP, so it passes on a clean machine. Emails (verification,
password reset) are printed to the terminal.

What does **not** work on this path, by design: carts and real products
need MongoDB and Saleor, so `/api/v1/products/` answers `503
catalogue_unavailable` rather than inventing data, and background jobs need
a broker. Add those in path B.

## 3B. Path B — full local stack

Install PostgreSQL 16+, MongoDB 7+ and Redis 7+, then create the database
role. One possible `psql` setup:

```sql
CREATE USER eve_user WITH PASSWORD 'choose-a-local-password';
```

```sql
CREATE DATABASE eve_db OWNER eve_user;
```

Now edit `.env` to point at the services you just installed:

```dotenv
DB_ENGINE=postgresql
DB_PASSWORD=the-password-you-just-chose
REDIS_URL=redis://localhost:6379/0
CELERY_BROKER_URL=redis://localhost:6379/1
MONGODB_SERVER_SELECTION_TIMEOUT_MS=5000
```

`DB_PASSWORD` must match the role exactly, or `migrate` fails with
`password authentication failed for user "eve_user"`.

```bash
python manage.py migrate
```

```bash
python manage.py ensure_indexes
```

```bash
python manage.py createsuperuser
```

```bash
python manage.py runserver
```

Background jobs need a worker and scheduler alongside `runserver`; see
`docs/BACKGROUND_JOBS.md`.

## 3C. Path C — Docker Compose

Compose supplies PostgreSQL, MongoDB, Redis, nginx and the app. Set these
in `.env` first — the container hostnames replace `localhost`:

```dotenv
DB_ENGINE=postgresql
DB_PASSWORD=choose-a-local-password
DJANGO_CSRF_TRUSTED_ORIGINS=http://localhost:8080
DB_SSLMODE=disable
```

```bash
docker compose up --build -d
```

```bash
docker compose exec web python manage.py migrate
```

```bash
docker compose exec web python manage.py ensure_indexes
```

Open <http://localhost:8080/>. Follow logs with `docker compose logs -f web
nginx`. `docker compose down` stops the stack; **do not** add `-v` unless
you want to erase the local databases.

This layout is production-*shaped*, not a production deployment: its
internal traffic and public endpoint use local non-TLS networking.

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
| Recovery evidence | `POSTGRES_BACKUP_LAST_SUCCESS_AT`, `MONGODB_BACKUP_LAST_SUCCESS_AT`, `RESTORE_TEST_LAST_SUCCESS_AT`, `BACKUP_ENCRYPTION_CONFIRMED`, `BACKUP_OFFSITE_CONFIRMED` | Updated only by trusted provider automation; verified by `audit_data_protection`. |

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
