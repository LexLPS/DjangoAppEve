from datetime import datetime, timedelta, timezone

from core.monitoring import MongoPoolLogger
from django.conf import settings
from pymongo import MongoClient

# Bounded server selection so health checks and requests fail fast when
# MongoDB is down instead of hanging for the 30s driver default; pool size
# is capped per process (size Mongo's max connections to workers × pool)
client = MongoClient(
    settings.MONGODB["HOST"],
    serverSelectionTimeoutMS=5000,
    maxPoolSize=settings.MONGODB.get("MAX_POOL_SIZE", 50),
    waitQueueTimeoutMS=2000,
    # Reports wait-queue pressure and pool exhaustion
    event_listeners=[MongoPoolLogger()],
)
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
    return list(products_collection.find({"cached_at": {"$gte": _freshness_cutoff()}}).limit(limit))


def get_stale_cached_products(limit: int = 50) -> list:
    """Return the newest entries regardless of TTL for degraded reads."""
    return list(products_collection.find({}).sort("cached_at", -1).limit(limit))


def get_cached_product(slug: str):
    return products_collection.find_one({"slug": slug, "cached_at": {"$gte": _freshness_cutoff()}})


def get_stale_cached_product(slug: str):
    return products_collection.find_one({"slug": slug})
