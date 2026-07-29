import hashlib
import json
import logging
import uuid

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from ecommerce.services.cart_service import clear_cart, get_cart

from .models import Order, WebhookEvent
from .services.saleor_checkout import CheckoutError, complete_checkout, create_checkout
from .services.saleor_webhooks import WebhookSignatureError, verify_saleor_signature

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

    # Orders go to the address on file — it must be verified first (threat
    # model R9); the checkout email is also what Saleor sends receipts to.
    from accounts.models import Profile
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not profile.email_verified:
        messages.error(
            request,
            "Please verify your email address before placing an order. "
            "You can resend the verification email from your profile.",
        )
        return redirect("profile")

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


@csrf_exempt
@require_POST
def saleor_webhook_view(request):
    """Receives payment-state events from Saleor.

    Signature-verified (Saleor detached RS256 JWS over the raw request body),
    idempotent (re-delivery of an already-applied state is a no-op),
    and transition-guarded (invalid state jumps are refused and logged).
    """
    try:
        verify_saleor_signature(
            request.body,
            request.headers.get("Saleor-Signature", ""),
        )
    except WebhookSignatureError:
        logger.warning("Saleor webhook rejected: bad or missing signature")
        return HttpResponse(status=401)

    try:
        payload = json.loads(request.body)
        order_id = payload["order"]["id"]
        event_type = payload["__typename"]
    except (ValueError, KeyError, TypeError):
        logger.warning("Saleor webhook rejected: malformed payload")
        return HttpResponse(status=400)

    # Store only the fields Eve needs; a Saleor subscription may later grow
    # to include customer data that must not be retained in the inbox.
    safe_payload = {"__typename": event_type, "order": {"id": order_id}}
    event, created = WebhookEvent.objects.get_or_create(
        fingerprint=hashlib.sha256(request.body).hexdigest(),
        defaults={
            "event_type": event_type,
            "saleor_order_id": order_id,
            "payload": safe_payload,
        },
    )

    if created:
        from .tasks import process_webhook_event

        def enqueue():
            try:
                process_webhook_event.delay(event.pk)
            except Exception:
                # The PostgreSQL inbox is already durable. Beat recovery will
                # republish this event when the broker is available again.
                logger.exception("Webhook durable but queue publication failed")

        transaction.on_commit(enqueue)

    return JsonResponse({"accepted": True, "duplicate": not created}, status=202)


@login_required
def payment_history_view(request):
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "payments/payment_history.html", {"orders": orders})
