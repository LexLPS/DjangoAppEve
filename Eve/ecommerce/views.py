import logging
from urllib.parse import urlparse

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .services.cart_service import add_to_cart, get_cart, remove_from_cart
from .services.mongo_client import cache_product, get_cached_product, get_cached_products
from .services.saleor_client import (
    SaleorAPIError,
    fetch_product_by_slug,
    fetch_products_from_saleor,
)

logger = logging.getLogger(__name__)

MAX_CART_QUANTITY = 20


def _is_valid_product(product) -> bool:
    """External data (Saleor / Mongo) is untrusted: require the fields the
    templates and cart depend on before rendering or caching anything."""
    return isinstance(product, dict) and all(
        isinstance(product.get(key), str) and product[key]
        for key in ("id", "name", "slug")
    )


def _safe_url(url):
    """Only plain http(s) URLs may be stored or rendered — anything else
    (javascript:, data:, malformed) is dropped."""
    if not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return url
    return None


def _sanitize_product(product: dict) -> dict:
    thumbnail = product.get("thumbnail")
    safe_thumb = _safe_url(thumbnail.get("url")) if isinstance(thumbnail, dict) else None
    product["thumbnail"] = {"url": safe_thumb} if safe_thumb else None

    media = product.get("media")
    if isinstance(media, list):
        product["media"] = [
            {"url": _safe_url(m.get("url"))}
            for m in media
            if isinstance(m, dict) and _safe_url(m.get("url"))
        ]
    return product


def _parse_quantity(raw):
    try:
        quantity = int(raw)
    except (TypeError, ValueError):
        return None
    if not 1 <= quantity <= MAX_CART_QUANTITY:
        return None
    return quantity


def product_catalogue_view(request):
    products = []
    catalogue_unavailable = False

    #  Try Mongo cache first (only entries still within the TTL)
    cached_products = get_cached_products(limit=50)
    if cached_products:
        products = [_sanitize_product(p) for p in cached_products if _is_valid_product(p)]

    #  If no fresh cache, try Saleor
    if not products:
        try:
            fetched = fetch_products_from_saleor(first=20)
            products = [_sanitize_product(p) for p in fetched if _is_valid_product(p)]
            logger.info("Saleor returned %d products (%d valid)", len(fetched), len(products))

            for product in products:
                cache_product(product)
        except SaleorAPIError:
            catalogue_unavailable = True
            logger.exception("Saleor catalogue fetch failed")

    #  If still nothing AND there was an error, use fallback mock data
    if not products and catalogue_unavailable:
        products = [
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

    context = {
        "products": products,
        "catalogue_unavailable": catalogue_unavailable,
    }
    return render(request, "ecommerce/product_catalogue.html", context)


# Negative cache for unknown slugs (threat model R3): without it every
# GET for a random slug costs one Saleor API call — an unauthenticated
# amplification vector against the upstream quota.
NEGATIVE_CACHE_SECONDS = 300


def _get_product_or_404(slug: str) -> dict:
    """Fresh cache first, then Saleor; validates before caching/returning.
    Slug misses are negatively cached."""
    miss_key = f"product-miss:{slug}"
    try:
        if cache.get(miss_key):
            raise Http404("Product not found")
    except Http404:
        raise
    except Exception:
        logger.exception("Negative cache unavailable")

    cached = get_cached_product(slug)
    if cached and _is_valid_product(cached):
        return _sanitize_product(cached)

    try:
        product = fetch_product_by_slug(slug)
    except SaleorAPIError:
        logger.exception("Saleor product fetch failed (slug=%s)", slug)
        raise Http404("Product not available at the moment") from None

    if not _is_valid_product(product):
        try:
            cache.set(miss_key, True, timeout=NEGATIVE_CACHE_SECONDS)
        except Exception:
            logger.exception("Negative cache unavailable")
        raise Http404("Product not found")

    product = _sanitize_product(product)
    cache_product(product)
    return product


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
