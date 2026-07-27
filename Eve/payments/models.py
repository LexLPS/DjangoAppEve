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
