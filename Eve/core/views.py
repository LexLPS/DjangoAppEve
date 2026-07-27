import logging

from django.contrib import messages
from django.db import connection
from django.http import JsonResponse
from django.shortcuts import redirect, render

from .forms import ContactForm
from .throttling import rate_limit

logger = logging.getLogger(__name__)

def landing_view(request):
    return render(request, "core/landing.html")

def health_view(request):
    """Monitoring probe: reports overall status only, never failure details."""
    healthy = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
    except Exception:
        logger.exception("Health check: PostgreSQL unreachable")
        healthy = False

    try:
        from ecommerce.services.mongo_client import client as mongo
        mongo.admin.command("ping")
    except Exception:
        logger.exception("Health check: MongoDB unreachable")
        healthy = False

    return JsonResponse(
        {"status": "ok" if healthy else "degraded"},
        status=200 if healthy else 503,
    )


@rate_limit("contact", limit=5, window_seconds=3600)
def contact_view(request):
    if request.method == "POST":
        form = ContactForm(request.POST)
        if form.is_valid():
            form.save()
            messages.success(request, "Thank you for reaching out.")
            return redirect("contact")
    else:
        form = ContactForm()
    return render(request, "core/contact.html", {"form": form})
