from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseNotAllowed
from .models import Order


@login_required
def checkout_view(request):
    # Checkout is intentionally disabled until a verified server-side payment
    # flow exists (Saleor checkout mutations + payment confirmation). Do not
    # re-enable order creation from client input without that flow.
    if request.method == "POST":
        return HttpResponseNotAllowed(["GET"])
    return render(request, "payments/checkout.html")


@login_required
def payment_history_view(request):
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "payments/payment_history.html", {"orders": orders})
