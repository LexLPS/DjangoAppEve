"""Server-side Saleor checkout.

The Mongo cart supplies only (variant_id, quantity) pairs. All prices are
recalculated by Saleor when the checkout is created — cart price fields are
display-only and never trusted for billing.
"""
import logging

from django.conf import settings
from ecommerce.services.saleor_client import SaleorAPIError, saleor_graphql

logger = logging.getLogger(__name__)


class CheckoutError(RuntimeError):
    """User-safe checkout failure; message may be shown to the customer."""


CHECKOUT_CREATE_MUTATION = """
mutation ($channel: String!, $email: String!, $lines: [CheckoutLineInput!]!, $metadata: [MetadataInput!]) {
  checkoutCreate(input: {channel: $channel, email: $email, lines: $lines, metadata: $metadata}) {
    checkout {
      id
      totalPrice {
        gross { amount currency }
      }
    }
    errors { field code message }
  }
}
"""

CHECKOUT_COMPLETE_MUTATION = """
mutation ($id: ID!) {
  checkoutComplete(id: $id) {
    order {
      id
      total { gross { amount currency } }
    }
    errors { field code message }
  }
}
"""


def build_lines(cart_items: list) -> list:
    """Translate cart items into Saleor checkout lines. Quantities are
    re-validated; items without a variant id cannot be purchased."""
    lines = []
    for item in cart_items:
        variant_id = item.get("variant_id")
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        if not variant_id or not 1 <= quantity <= 99:
            raise CheckoutError(
                "Your cart contains an item that can no longer be purchased. "
                "Please remove it and try again."
            )
        lines.append({"variantId": variant_id, "quantity": quantity})
    if not lines:
        raise CheckoutError("Your cart is empty.")
    return lines


def create_checkout(email: str, cart_items: list, *, idempotency_key: str = "") -> dict:
    """Create a Saleor checkout and return
    {"checkout_id", "total_amount", "total_currency"} with Saleor-calculated
    totals."""
    lines = build_lines(cart_items)

    try:
        # retry=False: mutations are not idempotent and must never auto-retry
        data = saleor_graphql(CHECKOUT_CREATE_MUTATION, {
            "channel": settings.SALEOR_CHANNEL,
            "email": email,
            "lines": lines,
            "metadata": (
                [{"key": "eve_idempotency_key", "value": idempotency_key}]
                if idempotency_key
                else []
            ),
        }, retry=False)
    except SaleorAPIError:
        logger.exception("Saleor checkoutCreate failed")
        raise CheckoutError("Checkout is temporarily unavailable. Please try again later.") from None

    payload = data["checkoutCreate"]
    if payload["errors"]:
        # Log field/code only — error messages may echo user input
        logger.error(
            "Saleor checkoutCreate errors: %s",
            [(e.get("field"), e.get("code")) for e in payload["errors"]],
        )
        raise CheckoutError(
            "Some items in your cart are unavailable. Please review your cart."
        )

    checkout = payload["checkout"]
    gross = checkout["totalPrice"]["gross"]
    return {
        "checkout_id": checkout["id"],
        "total_amount": gross["amount"],
        "total_currency": gross["currency"],
    }


def complete_checkout(checkout_id: str) -> dict:
    """Complete a Saleor checkout, returning {"order_id", "total_amount",
    "total_currency"}. Payment processing/confirmation happens on Saleor's
    side; the resulting order starts as pending locally and is promoted by
    signature-verified webhooks."""
    try:
        data = saleor_graphql(CHECKOUT_COMPLETE_MUTATION, {"id": checkout_id}, retry=False)
    except SaleorAPIError:
        logger.exception("Saleor checkoutComplete failed")
        raise CheckoutError(
            "We could not confirm the checkout result. Do not submit it again; "
            "we are reconciling it automatically."
        ) from None

    payload = data["checkoutComplete"]
    if payload["errors"]:
        logger.error(
            "Saleor checkoutComplete errors: %s",
            [(e.get("field"), e.get("code")) for e in payload["errors"]],
        )
        raise CheckoutError(
            "We could not confirm the checkout result. Do not submit it again; "
            "we are reconciling it automatically."
        )

    order = payload["order"]
    gross = order["total"]["gross"]
    return {
        "order_id": order["id"],
        "total_amount": gross["amount"],
        "total_currency": gross["currency"],
    }
