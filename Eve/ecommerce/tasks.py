"""Background Saleor catalogue refresh."""
import logging

from celery import shared_task

from .services.mongo_client import cache_product
from .services.saleor_client import fetch_products_from_saleor

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def refresh_catalogue(self, first: int = 50):
    # Reuse the same untrusted-data validation applied by web requests.
    from .views import _is_valid_product, _sanitize_product

    fetched = fetch_products_from_saleor(first=first)
    products = [_sanitize_product(p) for p in fetched if _is_valid_product(p)]
    for product in products:
        cache_product(product)
    logger.info(
        "Catalogue refresh cached %d product(s)",
        len(products),
        extra={"event": "catalogue_refresh", "products": len(products)},
    )
    return len(products)
