from django.urls import path
from . import views

urlpatterns = [
    path("checkout/", views.checkout_view, name="checkout"),
    path("history/", views.payment_history_view, name="payment_history"),
    path("webhooks/saleor/", views.saleor_webhook_view, name="saleor_webhook"),
]