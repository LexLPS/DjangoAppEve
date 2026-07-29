# Background jobs

Eve uses Celery with a Redis broker. PostgreSQL remains authoritative for
orders and accepted Saleor webhooks; Redis transports work but is never the
only record that a payment event was received.

## Services

Run exactly one Beat scheduler and one or more workers:

```bash
celery -A eve worker -l INFO -Q webhooks,orders,email,catalogue,maintenance,celery
celery -A eve beat -l INFO
```

On Railway create `worker` and `beat` services from the same repository and
Dockerfile as the web service, then override their start commands with the
commands above. Give all three services the same Django/PostgreSQL/MongoDB
variables. Set `CELERY_BROKER_URL` to a dedicated persistent Redis service;
do not use the cache Redis in staging or production.

The web service must run migrations before workers start consuming a new
release. Railway's existing pre-deploy command remains:

```bash
python manage.py migrate --noinput
```

## Queues and schedules

- `webhooks`: durable Saleor inbox processing and recovery every minute.
- `orders`: Saleor reconciliation with repair every hour. Checkout attempts
  carry an `eve_idempotency_key` Saleor metadata value so an order can be
  reattached to the exact user and local attempt after a lost response.
- `maintenance`: retention daily and resource sampling every minute.
- `email`: verification and account-lockout notifications.
- `catalogue`: Saleor-to-Mongo cache refresh every five minutes.

Only one Beat instance may run. Multiple Beat instances publish duplicate
scheduled jobs. The jobs are idempotent, but duplicates waste capacity.

## Delivery guarantees

The webhook request verifies Saleor's RS256 signature, validates the minimum
payload, inserts a SHA-256-deduplicated `WebhookEvent` in PostgreSQL, and then
publishes a task. If publication fails, the request is still safely accepted:
the minute recovery schedule republishes every pending inbox row.

Tasks accept identifiers, not credentials or customer data. Celery accepts
JSON only; pickle is disabled. Webhook processing locks both the inbox row and
the order row, so duplicate deliveries and duplicate tasks are safe.
Processed inbox events are retained for 90 days by default; pending events are
never purged automatically.

## Checkout recovery

Before `checkoutCreate`, Eve inserts a `CheckoutAttempt` in PostgreSQL. It
then journals the Saleor checkout ID before calling `checkoutComplete`. A
completion error is treated as an unknown outcome rather than proof of
failure, so the same idempotency key cannot create another Saleor checkout.

The hourly `reconcile_orders --fix` job reads `eve_idempotency_key` from
Saleor order metadata, creates a missing local `Order`, and marks its attempt
completed in one PostgreSQL transaction. Attempts older than
`CHECKOUT_RECOVERY_GRACE_SECONDS` (default 300) are marked unknown and emit a
`checkout_reconciliation` alert event. An unknown attempt with no matching
recent Saleor order requires manual review; do not delete or retry it blindly.

## Monitoring and alerts

`resource_snapshot` logs queue depths plus `webhook_pending` and
`webhook_oldest_seconds`. Page when a webhook remains pending for more than
five minutes or when the webhook queue grows continuously for ten minutes.
Alert on `task_failed` and watch `task_started`/`task_finished` durations.

## Deployment order

1. Provision persistent broker Redis with TLS and authentication.
2. Set `CELERY_BROKER_URL` on web, worker, and Beat.
3. Deploy the web release and run migrations.
4. Start one Beat service.
5. Start one worker, then scale workers horizontally after observing queue age.
6. Send a signed staging webhook and confirm inbox status becomes `processed`.
7. Stop the worker, send another webhook, restart it, and confirm recovery.
