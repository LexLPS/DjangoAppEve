# Production acceptance and launch checklist

Production acceptance is an evidence-gathering exercise, not a script that
silently charges or refunds a customer. Use a dedicated acceptance account, a
clearly labelled low-value product, an approved payment method, and a maximum
amount agreed by the owner. Never put card details in source, environment
variables, logs, screenshots, or command history.

## Preconditions

- [ ] Phase 7 load gate passed at 2x expected peak in staging.
- [ ] Phase 8 `audit_data_protection` passes and a restore drill is current.
- [ ] The same immutable release SHA passed staging and production CI.
- [ ] Production web has at least two replicas; worker is healthy; Beat is a singleton.
- [ ] Admin MFA, paging alerts, SMTP, Saleor RS256 webhook, and reconciliation are enabled.
- [ ] Rollback owner and incident channel are present for the test window.

## Low-value checkout

1. Enable checkout only for the controlled window.
2. Register/login as the acceptance user and verify the email message arrives.
3. Buy one low-value item through the real payment provider.
4. Record the Eve order ID, Saleor order ID, timestamp, release SHA, and payment
   provider transaction reference in the restricted launch record. Do not copy
   payment credentials or personal data.
5. Confirm Saleor shows the order and a confirmed/charged payment.
6. Confirm Eve receives the signed fully-paid webhook and Celery marks the
   durable inbox row processed.
7. Run:

   ```bash
   python manage.py verify_production_acceptance ORDER_ID --expect paid --json
   ```

## Webhook lifecycle

- [ ] Redeliver the fully-paid event; response is 202 with `duplicate=true`,
      one inbox fingerprint remains, and order state does not change.
- [ ] Deliver an out-of-order paid event after terminal refund/cancellation;
      it is retained as rejected and cannot regress state.
- [ ] Refund the low-value order in Saleor/payment provider and verify the
      signed refund event, Celery processing, and local refunded state.
- [ ] Run the verifier with `--expect refunded`.
- [ ] Use a separate unpaid acceptance order for cancellation; verify the
      cancellation event and run with `--expect cancelled`.

## Operational evidence

- [ ] Verification and password-reset emails arrive; no secrets or health data
      appear in delivery logs.
- [ ] Request, Saleor, task, webhook, and reconciliation events share usable
      timestamps/correlation context and remain redacted.
- [ ] Trigger a staging-only test alert and confirm the on-call acknowledges it.
- [ ] `reconcile_orders` reports clean after the acceptance lifecycle.
- [ ] No pending webhook older than five minutes and no uncertain checkout.
- [ ] Restore the latest PostgreSQL and Mongo cart backups into isolated scratch
      services and complete the documented restore checks within RTO.
- [ ] Exercise overlap rotation for a non-payment credential in staging, then
      Django signing-key fallback rotation; confirm old credentials are revoked.
- [ ] Run CI security gates, `audit_admins`, dependency audit, secret scan, and
      final threat-model review. Record reviewer and date.

## Launch decision

- [ ] Every item has an evidence link and named reviewer.
- [ ] Known risks have an owner, deadline, and explicit acceptance.
- [ ] Rollback was rehearsed and the previous deployment remains available.
- [ ] Product/payment owner, security reviewer, and release owner approve.
- [ ] `CHECKOUT_ENABLED=True` is approved for normal traffic.

Any failed item blocks launch. Disable checkout and reconcile before asking a
customer to retry an ambiguous payment.
