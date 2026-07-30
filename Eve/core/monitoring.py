"""Resource telemetry: connection pools and wait times.

Two mechanisms:
- A pymongo pool listener that reports slow connection check-outs as they
  happen (wait-queue pressure is invisible in request latency alone).
- `snapshot_resources()`, a point-in-time sample of PostgreSQL, Redis, and
  MongoDB pool usage, emitted by `manage.py sample_resources` during load
  tests and by cron in production.

Everything logs structured events; nothing here needs a metrics backend.
"""
import contextvars
import logging

from django.conf import settings
from django.db import connection
from django.utils import timezone
from pymongo import monitoring

logger = logging.getLogger("eve.resources")

# Milliseconds spent in MongoDB during the current request. Set per request
# by RequestMetricsMiddleware and reported as Server-Timing `mongo;dur`, so
# the share of a slow request that is MongoDB can be measured rather than
# inferred.
mongo_ms_var = contextvars.ContextVar("mongo_ms", default=0.0)


class MongoCommandTimer(monitoring.CommandListener):
    """Accumulates MongoDB command duration into the per-request counter."""

    def _record(self, event):
        try:
            mongo_ms_var.set(mongo_ms_var.get() + event.duration_micros / 1000)
        except Exception:  # telemetry must never break a query
            pass

    def succeeded(self, event):
        self._record(event)

    def failed(self, event):
        self._record(event)

    def started(self, event):
        pass

# Only report check-outs that actually waited — every request checks out a
# connection, so logging them all would drown the log
SLOW_CHECKOUT_MS = 50


class MongoPoolLogger(monitoring.ConnectionPoolListener):
    """Surfaces MongoDB wait-queue pressure and pool exhaustion."""

    def connection_checked_out(self, event):
        duration_ms = getattr(event, "duration", 0) * 1000
        if duration_ms >= SLOW_CHECKOUT_MS:
            logger.warning(
                "MongoDB connection wait %.0fms", duration_ms,
                extra={"event": "mongo_pool_wait", "wait_ms": round(duration_ms, 1)},
            )

    def connection_check_out_failed(self, event):
        # Reason is typically 'timeout' (waitQueueTimeoutMS) or 'poolClosed'
        logger.error(
            "MongoDB connection check-out failed: %s", event.reason,
            extra={"event": "mongo_pool_exhausted", "reason": str(event.reason)},
        )

    # Unused hooks required by the interface
    def pool_created(self, event): pass
    def pool_ready(self, event): pass
    def pool_cleared(self, event): pass
    def pool_closed(self, event): pass
    def connection_created(self, event): pass
    def connection_ready(self, event): pass
    def connection_closed(self, event): pass
    def connection_check_out_started(self, event): pass
    def connection_checked_in(self, event): pass


def _postgres_stats():
    """Connections this database has open, and the configured ceiling."""
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FILTER (WHERE state = 'active'), count(*) "
                "FROM pg_stat_activity WHERE datname = current_database()"
            )
            active, total = cursor.fetchone()
            cursor.execute("SELECT setting::int FROM pg_settings WHERE name = 'max_connections'")
            (max_connections,) = cursor.fetchone()
        return {"pg_active": active, "pg_total": total, "pg_max": max_connections}
    except Exception as exc:  # sampling must never break the caller
        return {"pg_error": type(exc).__name__}


def _redis_stats():
    """In-use vs available connections in this process's pool."""
    try:
        from django.core.cache import cache
        if not hasattr(cache, "_cache") or not hasattr(cache._cache, "get_client"):
            # LocMem (dev/test): no pool to report, not an error
            return {"redis_backend": "not-redis"}
        client = cache._cache.get_client()
        pool = client.connection_pool
        in_use = len(getattr(pool, "_in_use_connections", ()))
        available = len(getattr(pool, "_available_connections", ()))
        return {
            "redis_in_use": in_use,
            "redis_available": available,
            "redis_max": pool.max_connections,
        }
    except Exception as exc:
        return {"redis_error": type(exc).__name__}


def _mongo_stats():
    """Database-level Atlas statistics plus the configured client pool cap.

    Atlas application users commonly cannot run the cluster-wide
    ``serverStatus`` command. ``dbStats`` stays within the application's
    database and works with least-privilege database roles.
    """
    try:
        from ecommerce.services.mongo_client import mongo_db

        megabyte = 1024 * 1024
        database_stats = mongo_db.command({"dbStats": 1, "scale": megabyte})
        return {
            "mongo_collections": database_stats.get("collections"),
            "mongo_objects": database_stats.get("objects"),
            "mongo_data_mb": database_stats.get("dataSize"),
            "mongo_storage_mb": database_stats.get("storageSize"),
            "mongo_indexes": database_stats.get("indexes"),
            "mongo_index_mb": database_stats.get("indexSize"),
            "mongo_max_pool": settings.MONGODB.get("MAX_POOL_SIZE"),
        }
    except Exception as exc:
        return {"mongo_error": type(exc).__name__}


def _celery_stats():
    """Broker queue depth and durable webhook backlog age."""
    stats = {}
    try:
        from redis import Redis

        broker = Redis.from_url(
            settings.CELERY_BROKER_URL,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
        for queue in (
            "webhooks", "orders", "email", "catalogue", "maintenance", "celery"
        ):
            stats[f"queue_{queue}_depth"] = broker.llen(queue)
    except Exception as exc:
        stats["celery_broker_error"] = type(exc).__name__

    try:
        from payments.models import CheckoutAttempt, WebhookEvent

        pending = WebhookEvent.objects.filter(status=WebhookEvent.Status.PENDING)
        oldest = pending.order_by("received_at").values_list("received_at", flat=True).first()
        stats["webhook_pending"] = pending.count()
        stats["webhook_oldest_seconds"] = (
            round((timezone.now() - oldest).total_seconds(), 1) if oldest else 0
        )
        uncertain = CheckoutAttempt.objects.filter(
            state__in=[
                CheckoutAttempt.State.STARTED,
                CheckoutAttempt.State.CHECKOUT_CREATED,
                CheckoutAttempt.State.COMPLETING,
                CheckoutAttempt.State.UNKNOWN,
            ]
        )
        oldest_attempt = uncertain.order_by("created_at").values_list(
            "created_at", flat=True
        ).first()
        stats["checkout_uncertain"] = uncertain.count()
        stats["checkout_oldest_uncertain_seconds"] = (
            round((timezone.now() - oldest_attempt).total_seconds(), 1)
            if oldest_attempt
            else 0
        )
    except Exception as exc:
        stats["webhook_backlog_error"] = type(exc).__name__
    return stats


def snapshot_resources() -> dict:
    stats = {}
    stats.update(_postgres_stats())
    stats.update(_redis_stats())
    stats.update(_mongo_stats())
    stats.update(_celery_stats())
    return stats


def log_resource_snapshot():
    stats = snapshot_resources()
    logger.info(
        "resource snapshot %s", stats,
        extra={"event": "resource_snapshot", **stats},
    )
    return stats
