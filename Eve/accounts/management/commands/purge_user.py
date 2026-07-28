from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import PrivacyActionLog


class Command(BaseCommand):
    help = (
        "Erase a user's personal data everywhere: the Django user (profile and "
        "orders cascade with it) and the MongoDB cart. Supports data-deletion "
        "requests; the action is recorded in the privacy audit log. See "
        "docs/SECURITY_OPERATIONS.md."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Confirm deletion (required; the command refuses to run without it)",
        )
        parser.add_argument(
            "--performed-by", required=True,
            help="Name/identifier of the operator handling the request (audited)",
        )

    def handle(self, *args, **options):
        if not options["yes"]:
            raise CommandError("Refusing to delete without --yes.")

        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"User {options['username']!r} does not exist.") from None

        user_id = user.id
        username = user.username
        email = user.email
        with transaction.atomic():
            user.delete()  # Profile and Orders cascade via FK
            PrivacyActionLog.objects.create(
                action="delete",
                username=username,
                user_email=email,
                performed_by=options["performed_by"],
                details="User, profile, and orders deleted (FK cascade)",
            )

        from ecommerce.services.mongo_client import carts_collection
        result = carts_collection.delete_many({"user_id": user_id})

        self.stdout.write(
            self.style.SUCCESS(
                f"Purged user {username!r} (id={user_id}): relational data "
                f"deleted, {result.deleted_count} cart document(s) removed. "
                "Action recorded in the privacy audit log."
            )
        )
