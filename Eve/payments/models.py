from django.contrib.auth.models import User
from django.db import models


class Order(models.Model):
    """Local record of a Saleor order.

    PostgreSQL is the authoritative store for orders and payment state; the
    Mongo cart and Redis cache are convenience layers only. State changes
    arrive via signature-verified Saleor webhooks (payments/views.py).
    """

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PAID = "paid", "Paid"
        FAILED = "failed", "Failed"
        REFUNDED = "refunded", "Refunded"
        CANCELLED = "cancelled", "Cancelled"

    # Transitions a webhook may apply; anything else is logged and refused
    ALLOWED_TRANSITIONS = {
        Status.PENDING: {Status.PAID, Status.FAILED, Status.CANCELLED},
        Status.PAID: {Status.REFUNDED, Status.CANCELLED},
        Status.FAILED: {Status.PENDING, Status.CANCELLED},
        Status.REFUNDED: set(),
        Status.CANCELLED: set(),
    }

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="orders")
    saleor_order_id = models.CharField(max_length=100, unique=True)
    saleor_checkout_id = models.CharField(max_length=100, blank=True, default="")
    # One checkout attempt -> at most one order, even on double-submit
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=10, default="USD")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    status = models.CharField(
        max_length=50, choices=Status.choices, default=Status.PENDING
    )

    def can_transition_to(self, new_status: str) -> bool:
        try:
            return Order.Status(new_status) in self.ALLOWED_TRANSITIONS[
                Order.Status(self.status)
            ]
        except ValueError:
            return False

    def __str__(self):
        return f"Order {self.saleor_order_id} for {self.user.username} [{self.status}]"


class CheckoutAttempt(models.Model):
    """Durable journal around non-idempotent Saleor checkout mutations."""

    class State(models.TextChoices):
        STARTED = "started", "Started"
        CHECKOUT_CREATED = "checkout_created", "Checkout created"
        COMPLETING = "completing", "Completing"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed safely"
        UNKNOWN = "unknown", "Outcome unknown"

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="checkout_attempts"
    )
    idempotency_key = models.CharField(max_length=64, unique=True)
    cart_fingerprint = models.CharField(max_length=64)
    state = models.CharField(
        max_length=24, choices=State.choices, default=State.STARTED, db_index=True
    )
    saleor_checkout_id = models.CharField(max_length=100, blank=True, default="")
    saleor_order_id = models.CharField(max_length=100, blank=True, default="")
    last_error = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["state", "updated_at"],
                name="payments_ca_state_updated_idx",
            )
        ]

    def __str__(self):
        return f"Checkout attempt {self.pk} [{self.state}]"


class WebhookEvent(models.Model):
    """Durable, deduplicated inbox for signature-verified Saleor events."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        REJECTED = "rejected", "Rejected"

    fingerprint = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=64)
    saleor_order_id = models.CharField(max_length=100)
    payload = models.JSONField()
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING, db_index=True
    )
    attempts = models.PositiveIntegerField(default=0)
    last_error = models.CharField(max_length=100, blank=True, default="")
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["status", "received_at"],
                name="payments_we_status_2225cf_idx",
            )
        ]

    def __str__(self):
        return f"Saleor {self.event_type} for {self.saleor_order_id} [{self.status}]"
