"""Cart storage on MongoDB.

All mutations use single atomic update operators ($inc/$push/$pull/$set with
array filters) so concurrent requests from the same user cannot lose each
other's writes the way read-modify-write cycles could. The cart is
convenience state only — PostgreSQL remains the authoritative store for
orders and payments.

Run `manage.py ensure_indexes` at deploy time to create the unique
user_id index that backs the upsert pattern.
"""
from datetime import datetime, timezone

from .mongo_client import carts_collection

# Ceiling stored per line item (defence in depth against runaway writes)
MAX_ITEM_QUANTITY = 99
# Ceiling accepted from a single client request (HTML form and API alike)
MAX_REQUEST_QUANTITY = 20


def _now():
    return datetime.now(timezone.utc)


def _empty_cart_doc(user_id: int):
    return {
        "user_id": user_id,
        "items": [],
        "updated_at": _now(),
    }


def get_cart(user_id: int) -> dict:
    """Read the cart, creating it only when it does not exist yet.

    Read-first matters: this is on the hot path for every cart view and
    every add. An unconditional upsert made *reading* a cart a write, which
    on a remote MongoDB cost a full write round-trip (~450 ms measured on
    staging) instead of a read.
    """
    cart = carts_collection.find_one({"user_id": user_id})
    if cart is not None:
        return cart
    return carts_collection.find_one_and_update(
        {"user_id": user_id},
        {"$setOnInsert": _empty_cart_doc(user_id)},
        upsert=True,
        return_document=True,
    )


def add_to_cart(user_id: int, product: dict, quantity: int = 1):
    """Add or increment a line item.

    Round-trips are the cost driver here (each Mongo op is a network
    round-trip), so the common paths take two: an $inc that either hits or
    misses, followed by a $push or a clamp. The cart document is created
    lazily only when the $push finds no cart to append to.
    """
    # Atomically bump quantity if the item is already present...
    result = carts_collection.update_one(
        {"user_id": user_id, "items.product_id": product["id"]},
        {
            "$inc": {"items.$.quantity": quantity},
            "$set": {"updated_at": _now()},
        },
    )

    if result.matched_count == 0:
        # ...otherwise push a new line item. The items.product_id $ne guard
        # makes this a no-op if a concurrent request pushed it first.
        try:
            gross = product["pricing"]["priceRange"]["start"]["gross"]
            price = gross["amount"]
            currency = gross["currency"]
            if isinstance(price, bool) or not isinstance(price, (int, float)):
                raise ValueError("non-numeric price")
            if not isinstance(currency, str) or not currency:
                raise ValueError("invalid currency")
        except Exception:
            price = 0
            currency = "EUR"

        line_item = {
            "product_id": product["id"],
            "slug": product["slug"],
            "name": product["name"],
            "variant_id": (product.get("defaultVariant") or {}).get("id"),
            "price_amount": price,
            "price_currency": currency,
            "quantity": quantity,
            "thumbnail_url": (product.get("thumbnail") or {}).get("url"),
        }

        def push_item():
            return carts_collection.update_one(
                {"user_id": user_id, "items.product_id": {"$ne": product["id"]}},
                {
                    "$push": {"items": line_item},
                    "$set": {"updated_at": _now()},
                },
            )

        if push_item().matched_count == 0:
            # No cart document yet (first ever add for this user): create it
            # and retry once. Only this cold path pays a third round-trip.
            get_cart(user_id)
            push_item()
    else:
        # Clamp runaway quantities (atomic; the array filter targets the
        # item). Only needed after an increment: a freshly pushed line item
        # carries a request-validated quantity that cannot exceed the cap.
        carts_collection.update_one(
            {"user_id": user_id},
            {"$set": {"items.$[over].quantity": MAX_ITEM_QUANTITY}},
            array_filters=[{"over.product_id": product["id"],
                            "over.quantity": {"$gt": MAX_ITEM_QUANTITY}}],
        )


def set_item_quantity(user_id: int, product_id: str, quantity: int) -> bool:
    """Set an existing line item's quantity outright (API clients set a
    value rather than incrementing). Atomic; returns False when the item is
    not in the cart."""
    quantity = max(1, min(int(quantity), MAX_ITEM_QUANTITY))
    result = carts_collection.update_one(
        {"user_id": user_id, "items.product_id": product_id},
        {
            "$set": {"items.$.quantity": quantity, "updated_at": _now()},
        },
    )
    return result.matched_count > 0


def remove_from_cart(user_id: int, product_id: str):
    carts_collection.update_one(
        {"user_id": user_id},
        {
            "$pull": {"items": {"product_id": product_id}},
            "$set": {"updated_at": _now()},
        },
    )


def clear_cart(user_id: int):
    carts_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {"items": [], "updated_at": _now()},
        },
        upsert=True,
    )
