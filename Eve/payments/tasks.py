"""Idempotent background processing for payment and Saleor work."""
import logging

from celery import shared_task
from django.core.management import call_command
from django.db import OperationalError, transaction
from django.utils import timezone

from .models import Order, WebhookEvent

logger = logging.getLogger(__name__)

EVENT_TO_STATUS = {
    "OrderFullyPaid": Order.Status.PAID,
    "OrderRefunded": Order.Status.REFUNDED,
    "OrderFullyRefunded": Order.Status.REFUNDED,
    "OrderCancelled": Order.Status.CANCELLED,
}


@shared_task(
    bind=True,
    autoretry_for=(OperationalError,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def process_webhook_event(self, event_id: int):
    """Apply one durable inbox event exactly once at the domain layer."""
    with transaction.atomic():
        event = WebhookEvent.objects.select_for_update().get(pk=event_id)
        if event.status != WebhookEvent.Status.PENDING:
            return event.status

        event.attempts += 1
        new_status = EVENT_TO_STATUS.get(event.event_type)
        if new_status is None:
            event.status = WebhookEvent.Status.IGNORED
            event.processed_at = timezone.now()
            event.save(update_fields=["attempts", "status", "processed_at"])
            return event.status

        order = Order.objects.select_for_update().filter(
            saleor_order_id=event.saleor_order_id
        ).first()
        if order is None:
            event.last_error = "unknown_order"
            event.save(update_fields=["attempts", "last_error"])
            logger.warning("Saleor webhook for unknown order %s", event.saleor_order_id)
            # Keep pending: reconciliation may recreate an order that exists
            # in Saleor but was not committed locally. Beat will retry it.
            return WebhookEvent.Status.PENDING

        if order.status != new_status:
            if not order.can_transition_to(new_status):
                event.status = WebhookEvent.Status.REJECTED
                event.last_error = "invalid_transition"
                event.processed_at = timezone.now()
                event.save(
                    update_fields=["attempts", "status", "last_error", "processed_at"]
                )
                logger.error(
                    "Saleor webhook refused transition %s -> %s for order %s",
                    order.status,
                    new_status,
                    event.saleor_order_id,
                )
                return event.status
            order.status = new_status
            order.save(update_fields=["status", "updated_at"])

        event.status = WebhookEvent.Status.PROCESSED
        event.processed_at = timezone.now()
        event.save(update_fields=["attempts", "status", "processed_at"])

    logger.info("Order %s -> %s via webhook", event.saleor_order_id, new_status)
    return event.status


@shared_task
def recover_pending_webhooks(batch_size: int = 100):
    """Republish durable events missed while the broker or workers were down."""
    ids = list(
        WebhookEvent.objects.filter(status=WebhookEvent.Status.PENDING)
        .order_by("received_at")
        .values_list("id", flat=True)[:batch_size]
    )
    for event_id in ids:
        process_webhook_event.delay(event_id)
    logger.info(
        "Webhook recovery queued %d event(s)",
        len(ids),
        extra={"event": "celery_recovery", "queued": len(ids)},
    )
    return len(ids)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def reconcile_orders(self):
    call_command("reconcile_orders", "--fix")
