import logging

from core.throttling import rate_limit
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core import signing
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .forms import RegistrationForm
from .models import Profile

# Lockout lives in a service shared with the API token endpoint, so a single
# implementation guards every password-accepting entry point.
from .services import lockout

logger = logging.getLogger(__name__)

EMAIL_TOKEN_SALT = "accounts.email-verification"
EMAIL_TOKEN_MAX_AGE = 60 * 60 * 24 * 3  # 3 days


def _send_verification_email(user):
    from .tasks import send_verification_email

    send_verification_email.delay(user.pk)


@rate_limit("register", limit=5, window_seconds=3600)
def register_view(request):
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            Profile.objects.get_or_create(user=user)
            try:
                _send_verification_email(user)
            except Exception:
                logger.exception("Could not send verification email")
            login(request, user)
            return redirect("landing")
    else:
        form = RegistrationForm()
    return render(request, "accounts/register.html", {"form": form})


@rate_limit("login", limit=5, window_seconds=300)
def login_view(request):
    # If user is already logged in, no need to show login form
    if request.user.is_authenticated:
        return redirect("landing")

    if request.method == "POST":
        username = request.POST.get("username", "")
        if username and lockout.is_locked(username):
            messages.error(
                request,
                "This account is temporarily locked after repeated failed "
                "sign-in attempts. Please try again later.",
            )
            return render(
                request, "accounts/login.html",
                {"form": AuthenticationForm()}, status=429,
            )

        form = AuthenticationForm(request, data=request.POST)
        if form.is_valid():
            lockout.clear_failures(username)
            login(request, form.get_user())
            return redirect("product_catalogue")
        if username:
            lockout.register_failure(username)
    else:
        form = AuthenticationForm()
    return render(request, "accounts/login.html", {"form": form})


@require_POST
def logout_view(request):
    # POST-only with CSRF so third-party pages cannot force a logout
    if request.user.is_authenticated:
        logout(request)
    return render(request, "accounts/logged_out.html")


@login_required
def profile_view(request):
    profile, created = Profile.objects.get_or_create(user=request.user)
    return render(request, "accounts/profile.html", {"profile": profile})


def verify_email_view(request, token):
    try:
        data = signing.loads(token, salt=EMAIL_TOKEN_SALT, max_age=EMAIL_TOKEN_MAX_AGE)
        user = User.objects.get(pk=data["uid"])
    except (signing.BadSignature, KeyError, User.DoesNotExist):
        return render(
            request, "accounts/email_verification.html",
            {"success": False}, status=400,
        )

    profile, _ = Profile.objects.get_or_create(user=user)
    if not profile.email_verified:
        profile.email_verified = True
        profile.save(update_fields=["email_verified"])
    return render(request, "accounts/email_verification.html", {"success": True})


@login_required
@rate_limit("resend-verification", limit=3, window_seconds=3600)
@require_POST
def resend_verification_view(request):
    profile, _ = Profile.objects.get_or_create(user=request.user)
    if not profile.email_verified:
        try:
            _send_verification_email(request.user)
            messages.success(request, "Verification email sent.")
        except Exception:
            logger.exception("Could not send verification email")
            messages.error(request, "Could not send the email. Try again later.")
    return redirect("profile")
