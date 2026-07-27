from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = (
        "Erase a user's personal data everywhere: the Django user (profile and "
        "orders cascade with it) and the MongoDB cart. Supports data-deletion "
        "requests; see docs/SECURITY_OPERATIONS.md."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm deletion (required; the command refuses to run without it)",
        )

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Refusing to delete without --yes.")

        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"User {options['username']!r} does not exist.")

        user_id = user.id
        with transaction.atomic():
            user.delete()  # Profile and Orders cascade via FK

        from ecommerce.services.mongo_client import carts_collection
        result = carts_collection.delete_many({"user_id": user_id})

        self.stdout.write(
            self.style.SUCCESS(
                f"Purged user {options['username']!r} (id={user_id}): relational data "
                f"deleted, {result.deleted_count} cart document(s) removed."
            )
        )
