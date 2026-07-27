from django.core.management.base import BaseCommand

from ecommerce.services.mongo_client import carts_collection, products_collection


class Command(BaseCommand):
    help = "Create MongoDB indexes. Run once at every deploy (idempotent)."

    def handle(self, *args, **options):
        # One cart per user; backs the atomic upsert in cart_service
        carts_collection.create_index("user_id", unique=True)
        # Cache lookups by slug and TTL-window filters by cached_at
        products_collection.create_index("slug")
        products_collection.create_index("cached_at")
        products_collection.create_index("id", unique=True)
        self.stdout.write(self.style.SUCCESS("MongoDB indexes ensured."))
