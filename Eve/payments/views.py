import hashlib
import hmac
import json
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from ecommerce.services.cart_service import clear_cart, get_cart

from .models import Order
from .services.saleor_checkout import CheckoutError, complete_checkout, create_checkout

logger = logging.getLogger(__name__)

IDEMPOTENCY_SESSION_KEY = "checkout_idempotency_key"


@login_required
def checkout_view(request):
    # Fail closed: checkout stays disabled until the Saleor integration tests
    # pass and CHECKOUT_ENABLED is explicitly set (with a webhook secret).
    if not settings.CHECKOUT_ENABLED:
        if request.method == "POST":
            return HttpResponseNotAllowed(["GET"])
        return render(request, "payments/checkout.html", {"checkout_enabled": False})

    if request.method != "POST":
        # A fresh idempotency key per checkout page view: double-submits of
        # the same form can only ever produce one order.
        request.session[IDEMPOTENCY_SESSION_KEY] = uuid.uuid4().hex
        cart = get_cart(request.user.id)
        return render(request, "payments/checkout.html", {
            "checkout_enabled": True,
            "items": cart["items"],
        })

    idempotency_key = request.session.get(IDEMPOTENCY_SESSION_KEY)
    if not idempotency_key:
        return redirect("checkout")

    existing = Order.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        messages.info(request, "This order was already placed.")
        return redirect("payment_history")

    cart = get_cart(request.user.id)
    try:
        # Prices are recalculated by Saleor here — cart amounts are not used
        checkout = create_checkout(request.user.email, cart["items"])
        result = complete_checkout(checkout["checkout_id"])
    except CheckoutError as exc:
        messages.error(request, str(exc))
        return redirect("checkout")

    try:
        Order.objects.create(
            user=request.user,
            saleor_order_id=result["order_id"],
            saleor_checkout_id=checkout["checkout_id"],
            idempotency_key=idempotency_key,
            total_amount=result["total_amount"],
            currency=result["total_currency"],
            status=Order.Status.PENDING,
        )
    except IntegrityError:
        # Concurrent double-submit lost the race — the order exists
        logger.info("Duplicate checkout suppressed (key=%s)", idempotency_key)

    del request.session[IDEMPOTENCY_SESSION_KEY]
    clear_cart(request.user.id)
    messages.success(request, "Order placed. Payment confirmation may take a moment.")
    return redirect("payment_history")


def _verify_webhook_signature(request) -> bool:
    secret = settings.SALEOR_WEBHOOK_SECRET
    if not secret:
        return False
    signature = request.headers.get("Saleor-Signature", "")
    expected = hmac.new(secret.encode(), request.body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


@csrf_exempt
@require_POST
def saleor_webhook_view(request):
    """Receives payment-state events from Saleor.

    Signature-verified (HMAC-SHA256 over the raw body with the webhook
    secret), idempotent (re-delivery of an already-applied state is a no-op),
    and transition-guarded (invalid state jumps are refused and logged).
    """
    if not _verify_webhook_signature(request):
        logger.warning("Saleor webhook rejected: bad or missing signature")
        return HttpResponse(status=401)

    try:
        payload = json.loads(request.body)
        order_id = payload["order"]["id"]
        event_type = payload["event_type"]
    except (ValueError, KeyError, TypeError):
        logger.warning("Saleor webhook rejected: malformed payload")
        return HttpResponse(status=400)

    event_to_status = {
        "order_fully_paid": Order.Status.PAID,
        "order_payment_failed": Order.Status.FAILED,
        "order_refunded": Order.Status.REFUNDED,
        "order_cancelled": Order.Status.CANCELLED,
    }
    new_status = event_to_status.get(event_type)
    if new_status is None:
        logger.info("Saleor webhook: ignoring event %s", event_type)
        return JsonResponse({"handled": False})

    order = Order.objects.filter(saleor_order_id=order_id).first()
    if order is None:
        # Unknown order: acknowledge so Saleor stops retrying, but log it
        logger.warning("Saleor webhook for unknown order %s", order_id)
        return JsonResponse({"handled": False})

    if order.status == new_status:
        return JsonResponse({"handled": True})  # idempotent re-delivery

    if not order.can_transition_to(new_status):
        logger.error(
            "Saleor webhook refused transition %s -> %s for order %s",
            order.status, new_status, order_id,
        )
        return JsonResponse({"handled": False}, status=409)

    order.status = new_status
    order.save(update_fields=["status", "updated_at"])
    logger.info("Order %s -> %s via webhook", order_id, new_status)
    return JsonResponse({"handled": True})


@login_required
def payment_history_view(request):
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "payments/payment_history.html", {"orders": orders})
