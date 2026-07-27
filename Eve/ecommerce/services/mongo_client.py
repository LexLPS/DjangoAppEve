from datetime import datetime, timedelta, timezone

from pymongo import MongoClient
from django.conf import settings

client = MongoClient(settings.MONGODB["HOST"])
mongo_db = client[settings.MONGODB["DB_NAME"]]

products_collection = mongo_db["products_cache"]
usage_logs_collection = mongo_db["usage_logs"]
carts_collection = mongo_db["carts"]


def _freshness_cutoff():
    ttl = settings.PRODUCT_CACHE_TTL_SECONDS
    return datetime.now(timezone.utc) - timedelta(seconds=ttl)


def cache_product(product: dict):
    products_collection.update_one(
        {"id": product["id"]},
        {"$set": {**product, "cached_at": datetime.now(timezone.utc)}},
        upsert=True,
    )


def get_cached_products(limit: int = 50) -> list:
    return list(
        products_collection.find({"cached_at": {"$gte": _freshness_cutoff()}}).limit(limit)
    )


def get_cached_product(slug: str):
    return products_collection.find_one(
        {"slug": slug, "cached_at": {"$gte": _freshness_cutoff()}}
    )
