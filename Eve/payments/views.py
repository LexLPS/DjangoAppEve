import hashlib
import json
import logging
import uuid

from core.cache_lock import CacheLeaseUnavailable, cache_lease
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpResponse, HttpResponseNotAllowed, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from ecommerce.services.cart_service import clear_cart, get_cart

from .models import CheckoutAttempt, Order, WebhookEvent
from .services.saleor_checkout import (
    CheckoutError,
    build_lines,
    complete_checkout,
    create_checkout,
)
from .services.saleor_webhooks import WebhookSignatureError, verify_saleor_signature

logger = logging.getLogger(__name__)

IDEMPOTENCY_SESSION_KEY = "checkout_idempotency_key"
CHECKOUT_LEASE_SECONDS = 60
MAX_WEBHOOK_BODY_BYTES = 64 * 1024
MAX_SALEOR_ORDER_ID_LENGTH = 255
ALLOWED_WEBHOOK_EVENTS = {
    "OrderFullyPaid",
    "OrderRefunded",
    "OrderFullyRefunded",
    "OrderCancelled",
}


def _place_order_once(*, user, cart, idempotency_key):
    """Serialize Saleor mutations and the local order write per user."""
    with cache_lease(f"checkout:user:{user.pk}", timeout=CHECKOUT_LEASE_SECONDS) as owner:
        if not owner:
            return None
        existing = Order.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing

        lines = build_lines(cart.get("items") or [])
        cart_fingerprint = hashlib.sha256(
            json.dumps(lines, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        attempt, created = CheckoutAttempt.objects.get_or_create(
            idempotency_key=idempotency_key,
            defaults={"user": user, "cart_fingerprint": cart_fingerprint},
        )
        if not created:
            if attempt.state == CheckoutAttempt.State.COMPLETED:
                return Order.objects.filter(idempotency_key=idempotency_key).first()
            if not (
                attempt.state == CheckoutAttempt.State.FAILED
                and not attempt.saleor_checkout_id
            ):
                return None
            attempt.state = CheckoutAttempt.State.STARTED
            attempt.last_error = ""
            attempt.cart_fingerprint = cart_fingerprint
            attempt.save(
                update_fields=["state", "last_error", "cart_fingerprint", "updated_at"]
            )

        logger.info(
            "Checkout attempt %s started",
            attempt.pk,
            extra={"event": "checkout_attempt", "attempt_id": attempt.pk, "state": "started"},
        )

        # Saleor recalculates prices; cart amounts are not trusted.
        try:
            checkout = create_checkout(
                user.email, cart["items"], idempotency_key=idempotency_key
            )
        except CheckoutError:
            attempt.state = CheckoutAttempt.State.FAILED
            attempt.last_error = "checkout_create_failed"
            attempt.save(update_fields=["state", "last_error", "updated_at"])
            raise

        attempt.saleor_checkout_id = checkout["checkout_id"]
        attempt.state = CheckoutAttempt.State.CHECKOUT_CREATED
        attempt.save(
            update_fields=["saleor_checkout_id", "state", "updated_at"]
        )
        attempt.state = CheckoutAttempt.State.COMPLETING
        attempt.save(update_fields=["state", "updated_at"])
        try:
            result = complete_checkout(checkout["checkout_id"])
        except CheckoutError:
            # The request may have reached Saleor even if Eve lost the
            # response. Never create another checkout for this key.
            attempt.state = CheckoutAttempt.State.UNKNOWN
            attempt.last_error = "checkout_complete_unknown"
            attempt.save(update_fields=["state", "last_error", "updated_at"])
            logger.error(
                "Checkout attempt %s outcome unknown",
                attempt.pk,
                extra={
                    "event": "checkout_attempt",
                    "attempt_id": attempt.pk,
                    "state": "unknown",
                },
            )
            raise

        with transaction.atomic():
            order = Order.objects.create(
                user=user,
                saleor_order_id=result["order_id"],
                saleor_checkout_id=checkout["checkout_id"],
                idempotency_key=idempotency_key,
                total_amount=result["total_amount"],
                currency=result["total_currency"],
                status=Order.Status.PENDING,
            )
            attempt.saleor_order_id = result["order_id"]
            attempt.state = CheckoutAttempt.State.COMPLETED
            attempt.completed_at = timezone.now()
            attempt.last_error = ""
            attempt.save(
                update_fields=[
                    "saleor_order_id",
                    "state",
                    "completed_at",
                    "last_error",
                    "updated_at",
                ]
            )
        logger.info(
            "Checkout attempt %s completed",
            attempt.pk,
            extra={"event": "checkout_attempt", "attempt_id": attempt.pk, "state": "completed"},
        )
        return order


@login_required
def checkout_view(request):
    if not settings.CHECKOUT_ENABLED:
        if request.method == "POST":
            return HttpResponseNotAllowed(["GET"])
        return render(request, "payments/checkout.html", {"checkout_enabled": False})

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
        request.session[IDEMPOTENCY_SESSION_KEY] = uuid.uuid4().hex
        cart = get_cart(request.user.id)
        return render(
            request,
            "payments/checkout.html",
            {"checkout_enabled": True, "items": cart["items"]},
        )

    idempotency_key = request.session.get(IDEMPOTENCY_SESSION_KEY)
    if not idempotency_key:
        return redirect("checkout")

    if Order.objects.filter(idempotency_key=idempotency_key).exists():
        messages.info(request, "This order was already placed.")
        return redirect("payment_history")

    try:
        order = _place_order_once(
            user=request.user,
            cart=get_cart(request.user.id),
            idempotency_key=idempotency_key,
        )
    except CacheLeaseUnavailable:
        logger.exception("Checkout coordination cache unavailable")
        messages.error(request, "Checkout is temporarily unavailable. Please try again.")
        return redirect("checkout")
    except CheckoutError as exc:
        messages.error(request, str(exc))
        return redirect("checkout")

    if order is None:
        messages.info(request, "Your checkout is already being processed.")
        return redirect("payment_history")

    request.session.pop(IDEMPOTENCY_SESSION_KEY, None)
    clear_cart(request.user.id)
    messages.success(request, "Order placed. Payment confirmation may take a moment.")
    return redirect("payment_history")


@csrf_exempt
@require_POST
def saleor_webhook_view(request):
    """Authenticate, validate, durably record, and enqueue Saleor events."""
    content_length = request.headers.get("Content-Length")
    try:
        if content_length is not None and int(content_length) > MAX_WEBHOOK_BODY_BYTES:
            return HttpResponse(status=413)
    except ValueError:
        return HttpResponse(status=400)

    raw_body = request.body
    if len(raw_body) > MAX_WEBHOOK_BODY_BYTES:
        return HttpResponse(status=413)
    if request.content_type != "application/json":
        return HttpResponse(status=415)

    try:
        verify_saleor_signature(
            raw_body,
            request.headers.get("Saleor-Signature", ""),
        )
    except WebhookSignatureError:
        logger.warning("Saleor webhook rejected: bad or missing signature")
        return HttpResponse(status=401)

    try:
        payload = json.loads(raw_body)
        order_id = payload["order"]["id"]
        event_type = payload["__typename"]
        if (
            not isinstance(order_id, str)
            or not order_id
            or len(order_id) > MAX_SALEOR_ORDER_ID_LENGTH
            or event_type not in ALLOWED_WEBHOOK_EVENTS
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        logger.warning("Saleor webhook rejected: malformed or unsupported payload")
        return HttpResponse(status=400)

    safe_payload = {"__typename": event_type, "order": {"id": order_id}}
    event, created = WebhookEvent.objects.get_or_create(
        fingerprint=hashlib.sha256(raw_body).hexdigest(),
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
                logger.exception("Webhook durable but queue publication failed")

        transaction.on_commit(enqueue)

    return JsonResponse({"accepted": True, "duplicate": not created}, status=202)


@login_required
def payment_history_view(request):
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    checkout_reconciliation_pending = CheckoutAttempt.objects.filter(
        user=request.user,
        state__in=[
            CheckoutAttempt.State.STARTED,
            CheckoutAttempt.State.CHECKOUT_CREATED,
            CheckoutAttempt.State.COMPLETING,
            CheckoutAttempt.State.UNKNOWN,
        ],
    ).exists()
    return render(
        request,
        "payments/payment_history.html",
        {
            "orders": orders,
            "checkout_reconciliation_pending": checkout_reconciliation_pending,
        },
    )
