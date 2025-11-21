from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from .models import Order

@login_required
def checkout_view(request):
    if request.method == "POST":
        # TODO: Call Saleor checkout/mutation with cart data
        saleor_order_id = "SO_TEMP_123"  # replace with real Saleor response
        order = Order.objects.create(
            user=request.user,
            saleor_order_id=saleor_order_id,
            total_amount=199.99,
            currency="USD",
            status="created",
        )
        messages.success(request, "Order created successfully.")
        return redirect("payment_history")
    return render(request, "payments/checkout.html")

@login_required
def payment_history_view(request):
    orders = Order.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "payments/payment_history.html", {"orders": orders})

