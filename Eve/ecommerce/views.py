"""Server-rendered storefront.

Catalogue reads live in services/catalogue.py so the HTML views and the
REST API (api/v1/) share one implementation; these views only translate
domain results into templates and redirects.
"""
import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .services.cart_service import (
    MAX_REQUEST_QUANTITY,
    add_to_cart,
    get_cart,
    remove_from_cart,
)
from .services.catalogue import (
    ProductNotFound,
    ProductUnavailable,
    get_product,
    list_products,
)

logger = logging.getLogger(__name__)

MAX_CART_QUANTITY = MAX_REQUEST_QUANTITY

# Shown only in the HTML storefront when the live catalogue is unreachable
# and nothing is cached. The API never invents products: it returns 503.
FALLBACK_PRODUCTS = [
    {
        "id": "1",
        "name": "Eve Horizon – Nature Escape",
        "slug": "eve-horizon-nature-escape",
        "description": "Guided VR walks through forests, beaches, and mountains.",
        "thumbnail": {"url": "https://via.placeholder.com/300x200?text=Nature"},
        "pricing": {
            "priceRange": {
                "start": {"gross": {"amount": 49.99, "currency": "EUR"}},
                "stop": {"gross": {"amount": 49.99, "currency": "EUR"}},
            }
        },
    },
    {
        "id": "2",
        "name": "Eve Home – Family Moments",
        "slug": "eve-home-family-moments",
        "description": "Recreate familiar home environments for comfort and nostalgia.",
        "thumbnail": {"url": "https://via.placeholder.com/300x200?text=Home"},
        "pricing": {
            "priceRange": {
                "start": {"gross": {"amount": 59.99, "currency": "EUR"}},
                "stop": {"gross": {"amount": 59.99, "currency": "EUR"}},
            }
        },
    },
]


def _parse_quantity(raw):
    try:
        quantity = int(raw)
    except (TypeError, ValueError):
        return None
    if not 1 <= quantity <= MAX_CART_QUANTITY:
        return None
    return quantity


def product_catalogue_view(request):
    products, catalogue_unavailable = list_products(limit=50)

    if not products and catalogue_unavailable:
        products = FALLBACK_PRODUCTS

    context = {
        "products": products,
        "catalogue_unavailable": catalogue_unavailable,
    }
    return render(request, "ecommerce/product_catalogue.html", context)


def _get_product_or_404(slug: str) -> dict:
    try:
        return get_product(slug)
    except ProductNotFound:
        raise Http404("Product not found") from None
    except ProductUnavailable:
        raise Http404("Product not available at the moment") from None


def product_detail_view(request, slug):
    product = _get_product_or_404(slug)
    return render(request, "ecommerce/product_detail.html", {"product": product})


@login_required
def cart_view(request):
    cart = get_cart(request.user.id)
    items = cart["items"]

    # Cart docs come from Mongo: tolerate missing/odd fields instead of 500ing
    total_amount = 0
    currency = None
    for item in items:
        try:
            amount = float(item.get("price_amount") or 0)
            quantity = int(item.get("quantity") or 0)
        except (TypeError, ValueError):
            continue
        total_amount += amount * quantity
        currency = item.get("price_currency") or currency

    context = {
        "cart": cart,
        "items": items,
        "total_amount": total_amount,
        "currency": currency,
    }
    return render(request, "ecommerce/cart.html", context)


@login_required
@require_POST
def add_to_cart_view(request, slug):
    quantity = _parse_quantity(request.POST.get("quantity", 1))
    if quantity is None:
        return HttpResponseBadRequest("Invalid quantity.")

    product = _get_product_or_404(slug)
    add_to_cart(request.user.id, product, quantity)

    return redirect("cart")


@login_required
@require_POST
def remove_from_cart_view(request, product_id):
    remove_from_cart(request.user.id, product_id)
    return redirect("cart")
