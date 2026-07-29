"""Read-only evidence gate for a deliberately placed acceptance order."""

import json

from django.core.management.base import BaseCommand, CommandError
from ecommerce.services.saleor_client import SaleorAPIError, saleor_graphql

from payments.models import CheckoutAttempt, Order, WebhookEvent

SALEOR_ORDER_QUERY = """
query ($id: ID!) {
  order(id: $id) {
    id
    status
    paymentStatus
  }
}
"""

EXPECTED_EVENTS = {
    Order.Status.PAID: {"OrderFullyPaid"},
    Order.Status.REFUNDED: {"OrderRefunded", "OrderFullyRefunded"},
    Order.Status.CANCELLED: {"OrderCancelled"},
}


class Command(BaseCommand):
    help = "Verify local, Saleor, checkout-journal, and webhook evidence for a real order."

    def add_arguments(self, parser):
        parser.add_argument("order_id")
        parser.add_argument(
            "--expect",
            required=True,
            choices=[Order.Status.PAID, Order.Status.REFUNDED, Order.Status.CANCELLED],
        )
        parser.add_argument("--json", action="store_true")

    def handle(self, *args, **options):
        order_id = options["order_id"]
        expected = options["expect"]
        failures = []
        order = Order.objects.filter(saleor_order_id=order_id).first()
        if order is None:
            failures.append("local order is missing")
        else:
            if order.status != expected:
                failures.append(f"local order status is {order.status}, expected {expected}")
            if not order.idempotency_key:
                failures.append("local order has no checkout idempotency key")
            attempt = CheckoutAttempt.objects.filter(
                idempotency_key=order.idempotency_key,
                state=CheckoutAttempt.State.COMPLETED,
                saleor_order_id=order_id,
            ).exists()
            if not attempt:
                failures.append("completed checkout journal evidence is missing")

        processed_events = set(
            WebhookEvent.objects.filter(
                saleor_order_id=order_id,
                status=WebhookEvent.Status.PROCESSED,
            ).values_list("event_type", flat=True)
        )
        if not processed_events.intersection(EXPECTED_EVENTS[expected]):
            failures.append(f"processed {expected} webhook evidence is missing")

        try:
            saleor_order = saleor_graphql(
                SALEOR_ORDER_QUERY, {"id": order_id}, retry=True
            ).get("order")
        except SaleorAPIError as exc:
            failures.append(f"Saleor verification failed ({exc.code})")
            saleor_order = None
        if not saleor_order or saleor_order.get("id") != order_id:
            failures.append("Saleor order is missing")
        elif expected == Order.Status.PAID and not saleor_order.get("paymentStatus"):
            failures.append("Saleor payment status is missing")

        result = {
            "order_id": order_id,
            "expected": expected,
            "passed": not failures,
            "checks": {
                "local_order": order is not None,
                "saleor_order": bool(saleor_order),
                "processed_events": sorted(processed_events),
            },
            "failures": failures,
        }
        output = json.dumps(result, sort_keys=True)
        if options["json"]:
            self.stdout.write(output)
        elif failures:
            for failure in failures:
                self.stderr.write(self.style.ERROR(failure))
        else:
            self.stdout.write(self.style.SUCCESS(f"Acceptance evidence passed for {order_id}."))
        if failures:
            raise CommandError("Production acceptance evidence is incomplete")
