"""Catalogue reads shared by the HTML views and the REST API.

One code path for both clients: cache-first with a single-flight refresh
lease, stale-while-error fallback, validation and URL sanitisation of
untrusted upstream data, and a negative cache for unknown slugs.

Callers translate the domain exceptions below into their own protocol
(Http404 for HTML, RFC-shaped JSON errors for the API).
"""
import logging
from urllib.parse import urlparse

from core.cache_lock import CacheLeaseUnavailable, cache_lease, wait_for_value
from django.core.cache import cache

from .mongo_client import (
    cache_product,
    get_cached_product,
    get_cached_products,
    get_stale_cached_product,
    get_stale_cached_products,
)
from .saleor_client import (
    SaleorAPIError,
    fetch_product_by_slug,
    fetch_products_from_saleor,
)

logger = logging.getLogger(__name__)

CACHE_REFRESH_LEASE_SECONDS = 15
CACHE_REFRESH_WAIT_SECONDS = 1.5
# Negative cache for unknown slugs (threat model R3): without it every
# GET for a random slug costs one Saleor API call — an unauthenticated
# amplification vector against the upstream quota.
NEGATIVE_CACHE_SECONDS = 300


class ProductNotFound(Exception):
    """The slug does not exist upstream."""


class ProductUnavailable(Exception):
    """Upstream is unreachable and no cached copy exists."""


def _fresh_products(limit=50):
    """Cache read that treats an unreachable cache as an empty cache.

    MongoDB being down must degrade to the Saleor/stale path, not raise:
    an unprotected read here turned a cache outage into a 500 on the
    catalogue, and made the app unusable on a clone without MongoDB.
    """
    try:
        return get_cached_products(limit=limit)
    except Exception:
        logger.exception("Product catalogue cache unavailable")
        return []


def _fresh_product(slug):
    try:
        return get_cached_product(slug)
    except Exception:
        logger.exception("Product cache unavailable (slug=%s)", slug)
        return None


def _stale_products(limit=50):
    try:
        return get_stale_cached_products(limit=limit)
    except Exception:
        logger.exception("Stale product catalogue cache unavailable")
        return []


def _stale_product(slug):
    try:
        return get_stale_cached_product(slug)
    except Exception:
        logger.exception("Stale product cache unavailable (slug=%s)", slug)
        return None


def _cache_product_safely(product):
    try:
        cache_product(product)
    except Exception:
        logger.exception("Product cache write unavailable (slug=%s)", product.get("slug"))


def is_valid_product(product) -> bool:
    """External data (Saleor / Mongo) is untrusted: require the fields the
    templates, serializers, and cart depend on before using anything."""
    return isinstance(product, dict) and all(
        isinstance(product.get(key), str) and product[key] for key in ("id", "name", "slug")
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


def sanitize_product(product: dict) -> dict:
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


def list_products(limit: int = 50):
    """Return (products, unavailable). `unavailable` is True when the live
    catalogue could not be reached; the products list may still hold stale
    entries served deliberately rather than failing."""
    products = []
    unavailable = False

    #  Try Mongo cache first (only entries still within the TTL)
    cached_products = _fresh_products(limit=limit)
    if cached_products:
        products = [sanitize_product(p) for p in cached_products if is_valid_product(p)]

    #  If no fresh cache, try Saleor behind a single-flight lease
    if not products:
        try:
            with cache_lease("catalogue:refresh", timeout=CACHE_REFRESH_LEASE_SECONDS) as owner:
                if owner:
                    fetched = fetch_products_from_saleor(first=20)
                    products = [sanitize_product(p) for p in fetched if is_valid_product(p)]
                    logger.info(
                        "Saleor returned %d products (%d valid)",
                        len(fetched),
                        len(products),
                    )
                    for product in products:
                        _cache_product_safely(product)
                else:
                    refreshed = wait_for_value(
                        lambda: _fresh_products(limit=limit) or None,
                        timeout=CACHE_REFRESH_WAIT_SECONDS,
                    )
                    products = [
                        sanitize_product(p)
                        for p in (refreshed or _stale_products(limit=limit))
                        if is_valid_product(p)
                    ]
        except (SaleorAPIError, CacheLeaseUnavailable):
            unavailable = True
            logger.exception("Saleor catalogue refresh unavailable")
            products = [
                sanitize_product(p) for p in _stale_products(limit=limit) if is_valid_product(p)
            ]

    return products, unavailable


def get_product(slug: str) -> dict:
    """Fresh cache first, then Saleor; validates before caching/returning.
    Slug misses are negatively cached.

    Raises ProductNotFound (unknown slug) or ProductUnavailable (upstream
    down and nothing cached).
    """
    miss_key = f"product-miss:{slug}"
    try:
        if cache.get(miss_key):
            raise ProductNotFound(slug)
    except ProductNotFound:
        raise
    except Exception:
        logger.exception("Negative cache unavailable")

    cached = _fresh_product(slug)
    if cached and is_valid_product(cached):
        return sanitize_product(cached)

    fetched_from_saleor = False
    try:
        with cache_lease(f"product:refresh:{slug}", timeout=CACHE_REFRESH_LEASE_SECONDS) as owner:
            if owner:
                product = fetch_product_by_slug(slug)
                fetched_from_saleor = True
            else:
                product = wait_for_value(
                    lambda: _fresh_product(slug),
                    timeout=CACHE_REFRESH_WAIT_SECONDS,
                ) or _stale_product(slug)
    except (SaleorAPIError, CacheLeaseUnavailable):
        logger.exception("Saleor product refresh unavailable (slug=%s)", slug)
        product = _stale_product(slug)
        if not is_valid_product(product):
            raise ProductUnavailable(slug) from None

    if not is_valid_product(product):
        try:
            cache.set(miss_key, True, timeout=NEGATIVE_CACHE_SECONDS)
        except Exception:
            logger.exception("Negative cache unavailable")
        raise ProductNotFound(slug)

    product = sanitize_product(product)
    if fetched_from_saleor:
        _cache_product_safely(product)
    return product
