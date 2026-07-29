"""Serializers for the v1 API.

Products and carts are dictionaries (Saleor / MongoDB), not ORM models, so
they use explicit read serializers that flatten the upstream shape into a
stable client contract — the API surface does not change when Saleor's
GraphQL response shape does.
"""
from accounts.models import Profile
from django.contrib.auth.models import User
from ecommerce.services.cart_service import MAX_REQUEST_QUANTITY
from payments.models import Order
from rest_framework import serializers


def _price(product):
    gross = (
        ((product.get("pricing") or {}).get("priceRange") or {}).get("start") or {}
    ).get("gross") or {}
    amount, currency = gross.get("amount"), gross.get("currency")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return None
    return {"amount": amount, "currency": currency or "EUR"}


class ProductSerializer(serializers.Serializer):
    """Read-only projection of an upstream product document."""

    def to_representation(self, product):
        return {
            "id": product.get("id"),
            "slug": product.get("slug"),
            "name": product.get("name"),
            "description": product.get("description") or "",
            "thumbnail_url": (product.get("thumbnail") or {}).get("url"),
            "price": _price(product),
            "purchasable": bool((product.get("defaultVariant") or {}).get("id")),
        }


class CartItemSerializer(serializers.Serializer):
    def to_representation(self, item):
        try:
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            quantity = 0
        try:
            unit_amount = float(item.get("price_amount") or 0)
        except (TypeError, ValueError):
            unit_amount = 0.0
        return {
            "product_id": item.get("product_id"),
            "slug": item.get("slug"),
            "name": item.get("name"),
            "thumbnail_url": item.get("thumbnail_url"),
            "quantity": quantity,
            "unit_price": {
                "amount": unit_amount,
                "currency": item.get("price_currency") or "EUR",
            },
            "line_total": round(unit_amount * quantity, 2),
        }


class CartSerializer(serializers.Serializer):
    """Totals are indicative only — Saleor recalculates them at checkout."""

    def to_representation(self, cart):
        items = [CartItemSerializer().to_representation(i) for i in cart.get("items") or []]
        currency = next(
            (i["unit_price"]["currency"] for i in items if i["unit_price"]["currency"]),
            "EUR",
        )
        return {
            "items": items,
            "item_count": sum(i["quantity"] for i in items),
            "estimated_total": {
                "amount": round(sum(i["line_total"] for i in items), 2),
                "currency": currency,
            },
            "updated_at": cart.get("updated_at"),
        }


class AddCartItemSerializer(serializers.Serializer):
    slug = serializers.SlugField(max_length=255)
    quantity = serializers.IntegerField(min_value=1, max_value=MAX_REQUEST_QUANTITY, default=1)


class UpdateCartItemSerializer(serializers.Serializer):
    quantity = serializers.IntegerField(min_value=1, max_value=MAX_REQUEST_QUANTITY)


class OrderSerializer(serializers.ModelSerializer):
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "saleor_order_id", "status", "total", "created_at", "updated_at"]
        read_only_fields = fields

    def get_total(self, order):
        return {"amount": str(order.total_amount), "currency": order.currency}


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "username", "email"]
        read_only_fields = fields


class ProfileSerializer(serializers.ModelSerializer):
    user = UserSerializer(read_only=True)

    class Meta:
        model = Profile
        # Data minimisation (threat model): hospital_name and room_number are
        # health-adjacent and are deliberately never exposed over the API.
        fields = ["user", "email_verified", "is_long_term_patient", "preferred_vr_mode"]
        read_only_fields = fields
