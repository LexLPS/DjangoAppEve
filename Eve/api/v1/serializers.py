"""Serializers for the v1 API.

Products and carts are dictionaries (Saleor / MongoDB), not ORM models, so
they use explicit read serializers that flatten the upstream shape into a
stable client contract — the API surface does not change when Saleor's
GraphQL response shape does.
"""
from accounts.models import Profile
from django.contrib.auth.models import User
from drf_spectacular.utils import extend_schema_field
from ecommerce.services.cart_service import MAX_REQUEST_QUANTITY
from payments.models import Order
from rest_framework import serializers


class MoneySerializer(serializers.Serializer):
    amount = serializers.FloatField()
    currency = serializers.CharField()


class ErrorBodySerializer(serializers.Serializer):
    code = serializers.CharField(help_text="Stable machine-readable error code.")
    message = serializers.CharField(help_text="Human-readable, safe to display.")
    details = serializers.DictField(required=False)
    request_id = serializers.CharField(help_text="Matches the X-Request-ID header.")


class ErrorSerializer(serializers.Serializer):
    """The envelope returned by every failing request."""

    error = ErrorBodySerializer()


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

    id = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    name = serializers.CharField(read_only=True)
    description = serializers.CharField(read_only=True)
    thumbnail_url = serializers.URLField(read_only=True, allow_null=True)
    price = MoneySerializer(read_only=True, allow_null=True)
    purchasable = serializers.BooleanField(
        read_only=True, help_text="False when the product has no buyable variant."
    )

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
    product_id = serializers.CharField(read_only=True)
    slug = serializers.SlugField(read_only=True)
    name = serializers.CharField(read_only=True)
    thumbnail_url = serializers.URLField(read_only=True, allow_null=True)
    quantity = serializers.IntegerField(read_only=True)
    unit_price = MoneySerializer(read_only=True)
    line_total = serializers.FloatField(read_only=True)

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

    items = CartItemSerializer(many=True, read_only=True)
    item_count = serializers.IntegerField(read_only=True)
    estimated_total = MoneySerializer(
        read_only=True, help_text="Indicative only; Saleor recalculates at checkout."
    )
    updated_at = serializers.DateTimeField(read_only=True, allow_null=True)

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


class OrderTotalSerializer(serializers.Serializer):
    """Money as an exact decimal string — never a float, for billing values."""

    amount = serializers.CharField()
    currency = serializers.CharField()


class OrderSerializer(serializers.ModelSerializer):
    total = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = ["id", "saleor_order_id", "status", "total", "created_at", "updated_at"]
        read_only_fields = fields

    @extend_schema_field(OrderTotalSerializer)
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


class ProductListResponseSerializer(serializers.Serializer):
    """Envelope for the catalogue listing."""

    count = serializers.IntegerField()
    degraded = serializers.BooleanField(
        help_text="True when stale cached data is served during an upstream outage."
    )
    results = ProductSerializer(many=True)


class TokenRequestSerializer(serializers.Serializer):
    username = serializers.CharField()
    password = serializers.CharField(style={"input_type": "password"})
    otp = serializers.CharField(
        required=False, allow_blank=True,
        help_text="Required when the account has a confirmed TOTP device.",
    )


class TokenResponseSerializer(serializers.Serializer):
    token = serializers.CharField()


class TokenIssueResponseSerializer(serializers.Serializer):
    """The raw token is returned once and cannot be recovered afterwards."""

    token = serializers.CharField(help_text="Store this now; it is never shown again.")
    expires_at = serializers.DateTimeField()


class TokenPairSerializer(serializers.Serializer):
    """Issued on login and on every refresh; store both values securely."""

    access = serializers.CharField(
        help_text="Short-lived; send as Authorization: Token <access>."
    )
    refresh = serializers.CharField(help_text="Single use: rotates on every refresh.")
    expires_in = serializers.IntegerField(help_text="Access token lifetime in seconds.")
    token_type = serializers.CharField()


class TokenRefreshRequestSerializer(serializers.Serializer):
    refresh = serializers.CharField()
