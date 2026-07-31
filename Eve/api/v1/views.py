"""v1 API views.

Every endpoint delegates to the same service layer the HTML storefront
uses (ecommerce.services.catalogue, ecommerce.services.cart_service,
payments.services.checkout) — no business logic is reimplemented here.
"""
import logging

from accounts.models import Profile
from core.cache_lock import CacheLeaseUnavailable
from django.conf import settings
from django.contrib.auth import authenticate
from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from ecommerce.services import cart_service
from ecommerce.services.catalogue import (
    ProductNotFound,
    ProductUnavailable,
    get_product,
    list_products,
)
from payments.models import Order
from payments.services.checkout import place_order_once, scoped_idempotency_key
from payments.services.saleor_checkout import CheckoutError
from rest_framework import mixins, status, viewsets
from rest_framework.authentication import SessionAuthentication
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from api.authentication import HashedTokenAuthentication
from api.errors import APIError
from api.models import ApiToken

from .serializers import (
    AddCartItemSerializer,
    CartSerializer,
    ErrorSerializer,
    OrderSerializer,
    ProductListResponseSerializer,
    ProductSerializer,
    ProfileSerializer,
    TokenIssueResponseSerializer,
    TokenRequestSerializer,
    UpdateCartItemSerializer,
)

logger = logging.getLogger(__name__)

MAX_IDEMPOTENCY_KEY_LENGTH = 64

# Reused in the OpenAPI schema so every documented failure shows the envelope
ERROR = ErrorSerializer
IDEMPOTENCY_KEY_PARAM = OpenApiParameter(
    name="Idempotency-Key",
    type=str,
    location=OpenApiParameter.HEADER,
    required=True,
    description=(
        "Client-generated key (e.g. a UUID, max 64 chars). Retrying with the "
        "same key returns the original order instead of placing a second one."
    ),
)


@extend_schema_view(
    list=extend_schema(
        tags=["products"],
        summary="List products",
        responses={200: ProductListResponseSerializer, 503: ERROR},
    ),
    retrieve=extend_schema(
        tags=["products"],
        summary="Retrieve a product by slug",
        responses={200: ProductSerializer, 404: ERROR, 503: ERROR},
    ),
)
class ProductViewSet(viewsets.ViewSet):
    """Public catalogue. Reads go through the cache-first service, so API
    traffic benefits from the same TTL cache, single-flight refresh, and
    negative caching as the storefront."""

    permission_classes = [AllowAny]
    lookup_field = "slug"
    lookup_value_regex = "[-a-zA-Z0-9_]+"

    def list(self, request):
        products, unavailable = list_products(limit=50)
        if not products and unavailable:
            # Never invent products: say the catalogue is degraded instead
            raise APIError(
                "catalogue_unavailable",
                "The product catalogue is temporarily unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        return Response({
            "count": len(products),
            "degraded": unavailable,  # stale data served during an outage
            "results": ProductSerializer(products, many=True).data,
        })

    def retrieve(self, request, slug=None):
        try:
            product = get_product(slug)
        except ProductNotFound:
            raise APIError(
                "product_not_found", "No product exists with that slug.",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from None
        except ProductUnavailable:
            raise APIError(
                "catalogue_unavailable",
                "The product catalogue is temporarily unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from None
        return Response(ProductSerializer(product).data)


@extend_schema(tags=["cart"])
class CartView(APIView):
    """The signed-in user's cart. Always keyed by the session/token user —
    a client can never address another user's cart."""

    permission_classes = [IsAuthenticated]

    @extend_schema(summary="Get the current cart",
                   responses={200: CartSerializer, 401: ERROR})
    def get(self, request):
        cart = cart_service.get_cart(request.user.id)
        return Response(CartSerializer(cart).data)

    @extend_schema(summary="Empty the cart", responses={204: None, 401: ERROR})
    def delete(self, request):
        cart_service.clear_cart(request.user.id)
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(tags=["cart"])
class CartItemsView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Add an item to the cart",
        request=AddCartItemSerializer,
        responses={
            201: CartSerializer, 400: ERROR, 401: ERROR, 404: ERROR,
            409: ERROR, 503: ERROR,
        },
    )
    def post(self, request):
        payload = AddCartItemSerializer(data=request.data)
        payload.is_valid(raise_exception=True)

        try:
            product = get_product(payload.validated_data["slug"])
        except ProductNotFound:
            raise APIError(
                "product_not_found", "No product exists with that slug.",
                status_code=status.HTTP_404_NOT_FOUND,
            ) from None
        except ProductUnavailable:
            raise APIError(
                "catalogue_unavailable",
                "The product catalogue is temporarily unavailable.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from None

        try:
            cart_service.add_to_cart(
                request.user.id, product, payload.validated_data["quantity"]
            )
        except cart_service.CartFullError as exc:
            raise APIError(
                "cart_full",
                f"A cart may hold at most {exc.args[0]} different products. "
                "Remove an item before adding another.",
                status_code=status.HTTP_409_CONFLICT,
            ) from None
        cart = cart_service.get_cart(request.user.id)
        return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)


@extend_schema(tags=["cart"])
class CartItemDetailView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        summary="Set an item's quantity",
        request=UpdateCartItemSerializer,
        responses={200: CartSerializer, 400: ERROR, 401: ERROR, 404: ERROR},
    )
    def patch(self, request, product_id):
        payload = UpdateCartItemSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        updated = cart_service.set_item_quantity(
            request.user.id, product_id, payload.validated_data["quantity"]
        )
        if not updated:
            raise APIError(
                "cart_item_not_found", "That item is not in your cart.",
                status_code=status.HTTP_404_NOT_FOUND,
            )
        return Response(CartSerializer(cart_service.get_cart(request.user.id)).data)

    @extend_schema(summary="Remove an item",
                   responses={204: None, 401: ERROR})
    def delete(self, request, product_id):
        cart_service.remove_from_cart(request.user.id, product_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckoutView(APIView):
    """Place an order.

    Requires an `Idempotency-Key` header: retrying with the same key returns
    the original order instead of charging twice, which is what makes this
    endpoint safe for mobile clients on flaky networks.
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "checkout"

    @extend_schema(
        tags=["checkout"],
        summary="Place an order",
        description=(
            "Creates an order from the current cart. Prices are recalculated "
            "by Saleor; cart amounts are never trusted for billing.\n\n"
            "* `201` — order created.\n"
            "* `200` — this Idempotency-Key was already used: the original "
            "order is returned and nothing is charged again.\n"
            "* `409 checkout_in_progress` — a request with this key is "
            "in flight. Do not retry; poll `/orders/`."
        ),
        parameters=[IDEMPOTENCY_KEY_PARAM],
        request=None,
        responses={
            201: OrderSerializer, 200: OrderSerializer, 400: ERROR,
            401: ERROR, 403: ERROR, 409: ERROR, 429: ERROR, 503: ERROR,
        },
    )
    def post(self, request):
        if not settings.CHECKOUT_ENABLED:
            raise APIError(
                "checkout_disabled",
                "Checkout is not available yet.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        profile, _ = Profile.objects.get_or_create(user=request.user)
        if not profile.email_verified:
            raise APIError(
                "email_not_verified",
                "Verify your email address before placing an order.",
                status_code=status.HTTP_403_FORBIDDEN,
            )

        key = (request.headers.get("Idempotency-Key") or "").strip()
        if not key or len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise APIError(
                "idempotency_key_required",
                "Provide an Idempotency-Key header (max "
                f"{MAX_IDEMPOTENCY_KEY_LENGTH} characters) so retries cannot "
                "create duplicate orders.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        existing = Order.objects.filter(
            idempotency_key=scoped_idempotency_key(request.user, key),
            user=request.user,
        ).first()
        if existing:
            # Idempotent replay: same key, same answer, no second charge
            return Response(OrderSerializer(existing).data, status=status.HTTP_200_OK)

        cart = cart_service.get_cart(request.user.id)
        try:
            order = place_order_once(
                user=request.user, cart=cart, idempotency_key=key
            )
        except CacheLeaseUnavailable:
            logger.exception("Checkout coordination cache unavailable")
            raise APIError(
                "checkout_unavailable",
                "Checkout is temporarily unavailable. Please try again.",
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            ) from None
        except CheckoutError as exc:
            raise APIError(
                "checkout_failed", str(exc),
                status_code=status.HTTP_409_CONFLICT,
            ) from None

        if order is None:
            # Another request holds the lease, or the attempt is mid-flight
            raise APIError(
                "checkout_in_progress",
                "This checkout is already being processed. Do not retry; "
                "poll your orders instead.",
                status_code=status.HTTP_409_CONFLICT,
            )

        cart_service.clear_cart(request.user.id)
        return Response(OrderSerializer(order).data, status=status.HTTP_201_CREATED)


@extend_schema_view(
    list=extend_schema(tags=["orders"], summary="List your orders",
                       responses={200: OrderSerializer(many=True), 401: ERROR}),
    retrieve=extend_schema(tags=["orders"], summary="Retrieve one of your orders",
                           responses={200: OrderSerializer, 401: ERROR, 404: ERROR}),
)
class OrderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Order history, scoped to the requesting user by the queryset itself."""

    permission_classes = [IsAuthenticated]
    serializer_class = OrderSerializer
    lookup_field = "saleor_order_id"
    lookup_value_regex = "[^/]+"

    def get_queryset(self):
        return Order.objects.filter(user=self.request.user).order_by("-created_at")


class ProfileView(APIView):
    permission_classes = [IsAuthenticated]

    @extend_schema(
        tags=["account"],
        summary="Get your profile",
        description=(
            "Health-adjacent fields (hospital, room) are deliberately not "
            "exposed through the API — data minimisation."
        ),
        responses={200: ProfileSerializer, 401: ERROR},
    )
    def get(self, request):
        profile, _ = Profile.objects.get_or_create(user=request.user)
        return Response(ProfileSerializer(profile).data)


@extend_schema(exclude=True)  # catch-all: not part of the documented surface
@api_view(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
@permission_classes([AllowAny])
def not_found_view(request, *args, **kwargs):
    """Catch-all for unmatched /api/v1/ paths, so clients always parse JSON
    instead of receiving Django's HTML 404 page."""
    raise APIError(
        "not_found", "No such endpoint in this API version.",
        status_code=status.HTTP_404_NOT_FOUND,
    )


@extend_schema(tags=["account"])
class TokenView(APIView):
    """Issue and revoke API tokens.

    Tokens are stored as SHA-256 digests with an expiry (threat model R11),
    so a database disclosure cannot yield usable credentials.
    """

    authentication_classes = [SessionAuthentication, HashedTokenAuthentication]
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "token"

    @extend_schema(
        summary="Exchange credentials for an API token",
        request=TokenRequestSerializer,
        responses={200: TokenIssueResponseSerializer, 400: ERROR, 429: ERROR},
    )
    def post(self, request):
        payload = TokenRequestSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        user = authenticate(
            request,
            username=payload.validated_data["username"],
            password=payload.validated_data["password"],
        )
        if user is None or not user.is_active:
            # Uniform failure: no signal about which part was wrong
            raise APIError(
                "invalid_credentials", "Incorrect username or password.",
                status_code=status.HTTP_400_BAD_REQUEST,
            )

        token, raw_token = ApiToken.issue(user, label=request.headers.get("User-Agent", ""))
        logger.info(
            "API token issued",
            extra={"event": "api_token_issued", "token_id": token.pk},
        )
        return Response(
            {"token": raw_token, "expires_at": token.expires_at},
            status=status.HTTP_200_OK,
        )

    @extend_schema(
        summary="Revoke tokens",
        description=(
            "Revokes the token used for this request, or every token for the "
            "account when authenticated with a session."
        ),
        request=None,
        responses={204: None, 401: ERROR},
    )
    def delete(self, request):
        if not request.user.is_authenticated:
            raise APIError(
                "authentication_required", "Authentication credentials were not provided.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        if isinstance(request.auth, ApiToken):
            deleted = ApiToken.objects.filter(pk=request.auth.pk).delete()[0]
        else:
            deleted = ApiToken.objects.filter(user=request.user).delete()[0]
        logger.info(
            "API tokens revoked",
            extra={"event": "api_token_revoked", "count": deleted},
        )
        return Response(status=status.HTTP_204_NO_CONTENT)
