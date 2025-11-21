from datetime import datetime, timezone
from .mongo_client import carts_collection


def _empty_cart_doc(user_id: int):
    return {
        "user_id": user_id,
        "items": [],
        "updated_at": datetime.now(timezone.utc),
    }


def get_cart(user_id: int) -> dict:
    cart = carts_collection.find_one({"user_id": user_id})
    if not cart:
        cart = _empty_cart_doc(user_id)
        carts_collection.insert_one(cart)
    return cart


def add_to_cart(user_id: int, product: dict, quantity: int = 1):
    cart = get_cart(user_id)
    items = cart["items"]

    # see if product already in cart
    for item in items:
        if item["product_id"] == product["id"]:
            item["quantity"] += quantity
            break
    else:
        # extract price (start gross) safely
        price = None
        currency = None
        try:
            gross = product["pricing"]["priceRange"]["start"]["gross"]
            price = gross["amount"]
            currency = gross["currency"]
        except Exception:
            price = 0
            currency = "EUR"

        items.append({
            "product_id": product["id"],
            "slug": product["slug"],
            "name": product["name"],
            "price_amount": price,
            "price_currency": currency,
            "quantity": quantity,
            "thumbnail_url": (product.get("thumbnail") or {}).get("url"),
        })

    carts_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "items": items,
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )


def remove_from_cart(user_id: int, product_id: str):
    cart = get_cart(user_id)
    items = [item for item in cart["items"] if item["product_id"] != product_id]
    carts_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "items": items,
                "updated_at": datetime.now(timezone.utc),
            }
        },
    )


def clear_cart(user_id: int):
    carts_collection.update_one(
        {"user_id": user_id},
        {
            "$set": {
                "items": [],
                "updated_at": datetime.now(timezone.utc),
            }
        },
        upsert=True,
    )