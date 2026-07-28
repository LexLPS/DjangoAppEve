import json

from core.models import ContactMessage
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from payments.models import Order

from accounts.models import PrivacyActionLog, Profile


class Command(BaseCommand):
    help = (
        "Export all personal data held for a user as JSON (data-subject "
        "access request). The export is written to stdout; the action is "
        "recorded in the privacy audit log."
    )

    def add_arguments(self, parser):
        parser.add_argument("username")
        parser.add_argument(
            "--performed-by", required=True,
            help="Name/identifier of the operator handling the request (audited)",
        )

    def handle(self, *args, **options):
        try:
            user = User.objects.get(username=options["username"])
        except User.DoesNotExist:
            raise CommandError(f"User {options['username']!r} does not exist.") from None

        profile = Profile.objects.filter(user=user).first()
        from ecommerce.services.mongo_client import carts_collection
        cart = carts_collection.find_one({"user_id": user.id}, {"_id": False})

        export = {
            "account": {
                "username": user.username,
                "email": user.email,
                "date_joined": user.date_joined,
                "last_login": user.last_login,
            },
            "profile": {
                "email_verified": profile.email_verified,
                "is_long_term_patient": profile.is_long_term_patient,
                "hospital_name": profile.hospital_name,
                "room_number": profile.room_number,
                "preferred_vr_mode": profile.preferred_vr_mode,
            } if profile else None,
            "orders": [
                {
                    "saleor_order_id": order.saleor_order_id,
                    "total_amount": str(order.total_amount),
                    "currency": order.currency,
                    "status": order.status,
                    "created_at": order.created_at,
                }
                for order in Order.objects.filter(user=user)
            ],
            "contact_messages": [
                {
                    "subject": message.subject,
                    "message": message.message,
                    "created_at": message.created_at,
                }
                for message in ContactMessage.objects.filter(email__iexact=user.email)
            ],
            "cart": cart,
        }

        PrivacyActionLog.objects.create(
            action="export",
            username=user.username,
            user_email=user.email,
            performed_by=options["performed_by"],
            details="Full data export (account, profile, orders, contact, cart)",
        )

        self.stdout.write(json.dumps(export, indent=2, default=str))
