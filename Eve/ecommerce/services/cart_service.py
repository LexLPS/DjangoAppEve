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

MAX_ITEM_QUANTITY = 99


def _now():
    return datetime.now(timezone.utc)


def _empty_cart_doc(user_id: int):
    return {
        "user_id": user_id,
        "items": [],
        "updated_at": _now(),
    }


def get_cart(user_id: int) -> dict:
    cart = carts_collection.find_one_and_update(
        {"user_id": user_id},
        {"$setOnInsert": _empty_cart_doc(user_id)},
        upsert=True,
        return_document=True,
    )
    return cart


def add_to_cart(user_id: int, product: dict, quantity: int = 1):
    get_cart(user_id)  # ensure the cart document exists

    # Atomically bump quantity if the item is already present…
    result = carts_collection.update_one(
        {"user_id": user_id, "items.product_id": product["id"]},
        {
            "$inc": {"items.$.quantity": quantity},
            "$set": {"updated_at": _now()},
        },
    )

    if result.matched_count == 0:
        # …otherwise push a new line item. The items.product_id $ne guard
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

        carts_collection.update_one(
            {"user_id": user_id, "items.product_id": {"$ne": product["id"]}},
            {
                "$push": {
                    "items": {
                        "product_id": product["id"],
                        "slug": product["slug"],
                        "name": product["name"],
                        "variant_id": (product.get("defaultVariant") or {}).get("id"),
                        "price_amount": price,
                        "price_currency": currency,
                        "quantity": quantity,
                        "thumbnail_url": (product.get("thumbnail") or {}).get("url"),
                    }
                },
                "$set": {"updated_at": _now()},
            },
        )

    # Clamp runaway quantities (also atomic; array filter targets the item)
    carts_collection.update_one(
        {"user_id": user_id},
        {"$set": {"items.$[over].quantity": MAX_ITEM_QUANTITY}},
        array_filters=[{"over.product_id": product["id"],
                        "over.quantity": {"$gt": MAX_ITEM_QUANTITY}}],
    )


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
