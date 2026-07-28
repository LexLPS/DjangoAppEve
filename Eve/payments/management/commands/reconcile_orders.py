"""Reconcile Saleor orders against the local Order table (threat model R4).

If the local Order write fails after checkoutComplete, Saleor owns an order
Eve doesn't know about — and its webhooks are then acknowledged-and-dropped.
Run this daily and alert when discrepancies appear.
"""
import logging

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from ecommerce.services.saleor_client import SaleorAPIError, saleor_graphql

from payments.models import Order

logger = logging.getLogger(__name__)

ORDERS_QUERY = """
query ($first: Int!) {
  orders(first: $first, sortBy: {field: CREATION_DATE, direction: DESC}) {
    edges {
      node {
        id
        userEmail
        total { gross { amount currency } }
      }
    }
  }
}
"""


class Command(BaseCommand):
    help = (
        "Compare recent Saleor orders with the local Order table and report "
        "orders Saleor knows about but Eve does not. --fix creates missing "
        "local records (status=pending; the next webhook re-delivery or a "
        "manual Saleor check promotes them). Run daily; alert on mismatches."
    )

    def add_arguments(self, parser):
        parser.add_argument("--first", type=int, default=100,
                            help="How many recent Saleor orders to compare (default 100)")
        parser.add_argument("--fix", action="store_true",
                            help="Create local records for missing orders with a matching user")

    def handle(self, *args, **options):
        try:
            data = saleor_graphql(ORDERS_QUERY, {"first": options["first"]})
            nodes = [edge["node"] for edge in data["orders"]["edges"]]
        except (SaleorAPIError, KeyError, TypeError) as exc:
            self.stderr.write(self.style.ERROR(f"Could not fetch Saleor orders: {exc}"))
            raise SystemExit(1) from None

        saleor_ids = [n["id"] for n in nodes if isinstance(n, dict) and n.get("id")]
        known = set(
            Order.objects.filter(saleor_order_id__in=saleor_ids)
            .values_list("saleor_order_id", flat=True)
        )
        missing = [n for n in nodes if n.get("id") and n["id"] not in known]

        if not missing:
            self.stdout.write(self.style.SUCCESS(
                f"Reconciliation clean: all {len(saleor_ids)} recent Saleor "
                "orders exist locally."
            ))
            return

        fixed = 0
        for node in missing:
            email = node.get("userEmail") or ""
            user = User.objects.filter(email__iexact=email).first() if email else None
            # Order ids are logged; emails are not (log hygiene)
            logger.warning(
                "Reconciliation: Saleor order %s missing locally (user %s)",
                node["id"], "matched" if user else "unmatched",
            )
            self.stdout.write(
                f"MISSING {node['id']} — local user "
                f"{'found' if user else 'NOT FOUND (manual review needed)'}"
            )
            if options["fix"] and user:
                gross = ((node.get("total") or {}).get("gross") or {})
                Order.objects.create(
                    user=user,
                    saleor_order_id=node["id"],
                    total_amount=str(gross.get("amount") or "0"),
                    currency=gross.get("currency") or "EUR",
                    status=Order.Status.PENDING,
                )
                fixed += 1

        summary = f"{len(missing)} Saleor order(s) missing locally"
        if options["fix"]:
            summary += f"; {fixed} recreated as pending"
        self.stdout.write(self.style.WARNING(summary))
