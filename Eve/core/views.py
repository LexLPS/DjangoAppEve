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

def liveness_view(request):
    """Liveness: the process is up and serving. No dependency checks — a
    dead database must not make the orchestrator restart healthy pods."""
    return JsonResponse({"status": "alive"})


def readiness_view(request):
    """Readiness: this pod may receive traffic. Checks the hard dependencies
    (PostgreSQL, MongoDB, cache); reports the Saleor circuit state without
    failing on it — the site degrades gracefully when Saleor is down.
    Status per check only, never failure details."""
    checks = {}

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        checks["postgresql"] = "ok"
    except Exception:
        logger.exception("Readiness: PostgreSQL unreachable")
        checks["postgresql"] = "fail"

    try:
        from ecommerce.services.mongo_client import client as mongo
        mongo.admin.command("ping")
        checks["mongodb"] = "ok"
    except Exception:
        logger.exception("Readiness: MongoDB unreachable")
        checks["mongodb"] = "fail"

    try:
        from django.core.cache import cache
        cache.set("readiness-probe", "1", timeout=5)
        checks["cache"] = "ok" if cache.get("readiness-probe") == "1" else "fail"
    except Exception:
        logger.exception("Readiness: cache unreachable")
        checks["cache"] = "fail"

    from ecommerce.services.saleor_client import _circuit_is_open
    saleor_circuit = "open" if _circuit_is_open() else "closed"

    ready = all(status == "ok" for status in checks.values())
    return JsonResponse(
        {
            "status": "ready" if ready else "degraded",
            "checks": checks,
            "saleor_circuit": saleor_circuit,
        },
        status=200 if ready else 503,
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
