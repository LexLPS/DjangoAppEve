
from django.contrib import admin

from .models import Order, WebhookEvent


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ("saleor_order_id", "user", "status", "total_amount", "currency")
    list_filter = ("status", "currency")
    search_fields = ("saleor_order_id", "user__username")
    readonly_fields = ("created_at", "updated_at")


@admin.register(WebhookEvent)
class WebhookEventAdmin(admin.ModelAdmin):
    list_display = (
        "event_type",
        "saleor_order_id",
        "status",
        "attempts",
        "received_at",
        "processed_at",
    )
    list_filter = ("status", "event_type")
    search_fields = ("saleor_order_id", "fingerprint")
    readonly_fields = (
        "fingerprint",
        "event_type",
        "saleor_order_id",
        "payload",
        "status",
        "attempts",
        "last_error",
        "received_at",
        "processed_at",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
