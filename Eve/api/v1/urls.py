"""v1 URL map.

Versioned in the path (`/api/v1/`) so a future v2 can change response
shapes without breaking clients pinned to v1.
"""
from django.urls import include, path, re_path
from rest_framework.authtoken import views as authtoken_views
from rest_framework.routers import DefaultRouter

from .views import (
    CartItemDetailView,
    CartItemsView,
    CartView,
    CheckoutView,
    OrderViewSet,
    ProductViewSet,
    ProfileView,
    not_found_view,
)

app_name = "v1"

router = DefaultRouter()
router.register(r"products", ProductViewSet, basename="product")
router.register(r"orders", OrderViewSet, basename="order")

urlpatterns = [
    # Token exchange for non-browser clients (mobile, server-to-server).
    # Browsers keep using session auth + CSRF.
    path("auth/token/", authtoken_views.obtain_auth_token, name="obtain-token"),

    path("cart/", CartView.as_view(), name="cart"),
    path("cart/items/", CartItemsView.as_view(), name="cart-items"),
    path("cart/items/<str:product_id>/", CartItemDetailView.as_view(), name="cart-item"),
    path("checkout/", CheckoutView.as_view(), name="checkout"),
    path("profile/", ProfileView.as_view(), name="profile"),
    path("", include(router.urls)),

    # Any other /api/v1/ path answers with the JSON error envelope — an API
    # client must never receive Django's HTML 404 page.
    re_path(r"^.*$", not_found_view, name="not-found"),
]
