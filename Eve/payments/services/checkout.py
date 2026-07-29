"""Checkout orchestration shared by the HTML views and the REST API.

Wraps the non-idempotent Saleor mutations in a per-user lease and a durable
CheckoutAttempt journal, so a double submit, a lost response, or a crash
mid-flight can never produce two orders. Both clients call
`place_order_once`; neither reimplements any of it.
"""
import hashlib
import json
import logging

from core.cache_lock import cache_lease
from django.db import transaction
from django.utils import timezone

from ..models import CheckoutAttempt, Order
from .saleor_checkout import CheckoutError, build_lines, complete_checkout, create_checkout

logger = logging.getLogger(__name__)

CHECKOUT_LEASE_SECONDS = 60


def place_order_once(*, user, cart, idempotency_key):
    """Serialize Saleor mutations and the local order write per user."""
    with cache_lease(f"checkout:user:{user.pk}", timeout=CHECKOUT_LEASE_SECONDS) as owner:
        if not owner:
            return None
        existing = Order.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

        lines = build_lines(cart.get("items") or [])
        cart_fingerprint = hashlib.sha256(
            json.dumps(lines, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        attempt, created = CheckoutAttempt.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={"user": user, "cart_fingerprint": cart_fingerprint},
        )
        if not created:
            if attempt.state == CheckoutAttempt.State.COMPLETED:
                return Order.objects.filter(idempotency_key=idempotency_key).first()
            if not (
                attempt.state == CheckoutAttempt.State.FAILED
                and not attempt.saleor_checkout_id
            ):
                return None
            attempt.state = CheckoutAttempt.State.STARTED
            attempt.last_error = ""
            attempt.cart_fingerprint = cart_fingerprint
            attempt.save(
                update_fields=["state", "last_error", "cart_fingerprint", "updated_at"]
            )

        logger.info(
            "Checkout attempt %s started",
            attempt.pk,
            extra={"event": "checkout_attempt", "attempt_id": attempt.pk, "state": "started"},
        )

        # Saleor recalculates prices; cart amounts are not trusted.
        try:
            checkout = create_checkout(
                user.email, cart["items"], idempotency_key=idempotency_key
            )
        except CheckoutError:
            attempt.state = CheckoutAttempt.State.FAILED
            attempt.last_error = "checkout_create_failed"
            attempt.save(update_fields=["state", "last_error", "updated_at"])
            raise

        attempt.saleor_checkout_id = checkout["checkout_id"]
        attempt.state = CheckoutAttempt.State.CHECKOUT_CREATED
        attempt.save(
            update_fields=["saleor_checkout_id", "state", "updated_at"]
        )
        attempt.state = CheckoutAttempt.State.COMPLETING
        attempt.save(update_fields=["state", "updated_at"])
        try:
            result = complete_checkout(checkout["checkout_id"])
        except CheckoutError:
            # The request may have reached Saleor even if Eve lost the
            # response. Never create another checkout for this key.
            attempt.state = CheckoutAttempt.State.UNKNOWN
            attempt.last_error = "checkout_complete_unknown"
            attempt.save(update_fields=["state", "last_error", "updated_at"])
            logger.error(
                "Checkout attempt %s outcome unknown",
                attempt.pk,
                extra={
                    "event": "checkout_attempt",
                    "attempt_id": attempt.pk,
                    "state": "unknown",
                },
            )
            raise

        with transaction.atomic():
            order = Order.objects.create(
                user=user,
                saleor_order_id=result["order_id"],
                saleor_checkout_id=checkout["checkout_id"],
                idempotency_key=idempotency_key,
                total_amount=result["total_amount"],
                currency=result["total_currency"],
                status=Order.Status.PENDING,
            )
            attempt.saleor_order_id = result["order_id"]
            attempt.state = CheckoutAttempt.State.COMPLETED
            attempt.completed_at = timezone.now()
            attempt.last_error = ""
            attempt.save(
                update_fields=[
                    "saleor_order_id",
                    "state",
                    "completed_at",
                    "last_error",
                    "updated_at",
                ]
            )
        logger.info(
            "Checkout attempt %s completed",
            attempt.pk,
            extra={"event": "checkout_attempt", "attempt_id": attempt.pk, "state": "completed"},
        )
        return order
