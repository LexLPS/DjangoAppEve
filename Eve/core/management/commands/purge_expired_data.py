from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone
from payments.models import WebhookEvent

from core.models import ContactMessage


class Command(BaseCommand):
    help = (
        "Enforce data retention: delete contact messages and abandoned carts "
        "older than the configured retention periods. Run daily (cron/CI); "
        "idempotent."
    )

    def handle(self, *args, **options):
        now = timezone.now()

        contact_cutoff = now - timedelta(days=settings.CONTACT_MESSAGE_RETENTION_DAYS)
        deleted_messages, _ = ContactMessage.objects.filter(
            created_at__lt=contact_cutoff
        ).delete()

        from ecommerce.services.mongo_client import carts_collection
        cart_cutoff = now - timedelta(days=settings.ABANDONED_CART_RETENTION_DAYS)
        cart_result = carts_collection.delete_many(
            {"updated_at": {"$lt": cart_cutoff}}
        )

        webhook_cutoff = now - timedelta(days=settings.WEBHOOK_EVENT_RETENTION_DAYS)
        deleted_webhooks, _ = WebhookEvent.objects.exclude(
            status=WebhookEvent.Status.PENDING
        ).filter(received_at__lt=webhook_cutoff).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Retention pass complete: {deleted_messages} contact message(s) "
                f"(older than {settings.CONTACT_MESSAGE_RETENTION_DAYS}d) and "
                f"{cart_result.deleted_count} abandoned cart(s) (older than "
                f"{settings.ABANDONED_CART_RETENTION_DAYS}d), and "
                f"{deleted_webhooks} processed webhook event(s) (older than "
                f"{settings.WEBHOOK_EVENT_RETENTION_DAYS}d) deleted."
            )
        )
