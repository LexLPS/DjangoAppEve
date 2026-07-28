import logging

from core.throttling import rate_limit
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.core import signing
from django.core.cache import cache
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from .forms import RegistrationForm
from .models import Profile

logger = logging.getLogger(__name__)

EMAIL_TOKEN_SALT = "accounts.email-verification"
EMAIL_TOKEN_MAX_AGE = 60 * 60 * 24 * 3  # 3 days

# Per-username lockout, complementing the per-IP rate limit: a distributed
# credential-stuffing run across many IPs still locks the targeted account.
LOCKOUT_THRESHOLD = 10
LOCKOUT_WINDOW_SECONDS = 900


def _lockout_key(username: str) -> str:
    return f"lockout:{username.strip().lower()}"


def _is_locked(username: str) -> bool:
    try:
        return (cache.get(_lockout_key(username)) or 0) >= LOCKOUT_THRESHOLD
    except Exception:
        logger.exception("Lockout cache unavailable; failing open")
        return False


def _record_failure(username: str) -> int:
    key = _lockout_key(username)
    try:
        if cache.add(key, 1, timeout=LOCKOUT_WINDOW_SECONDS):
            return 1
        return cache.incr(key)
    except Exception:
        logger.exception("Lockout cache unavailable; failure not recorded")
        return 0


def _notify_lockout(username: str):
    """Tell the real account owner their account was just locked (threat
    model R5: deliberate lockouts should not go unnoticed). Silent when the
    username doesn't exist — no enumeration signal."""
    user = User.objects.filter(username=username).first()
    if not user or not user.email:
        return
    try:
        send_mail(
            "Your Eve account was temporarily locked",
            f"Hi {user.username},\n\n"
            "There were repeated failed sign-in attempts on your account, so "
            "sign-in has been paused for about 15 minutes as a precaution.\n\n"
            "If this was you, simply wait and try again. If it was not you, "
            "we recommend resetting your password once sign-in is available "
            "again. Your password has not been changed.",
            None,  # DEFAULT_FROM_EMAIL
            [user.email],
        )
    except Exception:
        logger.exception("Could not send lockout notification")


def _clear_failures(username: str):
    try:
        cache.delete(_lockout_key(username))
    except Exception:
        logger.exception("Lockout cache unavailable; could not clear failures")


def _send_verification_email(request, user):
    token = signing.dumps({"uid": user.pk}, salt=EMAIL_TOKEN_SALT)
    url = request.build_absolute_uri(reverse("verify_email", args=[token]))
    send_mail(
        "Verify your Eve email address",
        f"Hi {user.username},\n\n"
        f"Please confirm your email address by opening this link:\n{url}\n\n"
        "The link is valid for 3 days. If you did not create an Eve account, "
        "you can ignore this message.",
        None,  # DEFAULT_FROM_EMAIL
        [user.email],
    )


@rate_limit("register", limit=5, window_seconds=3600)
def register_view(request):
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            Profile.objects.get_or_create(user=user)
            try:
                _send_verification_email(request, user)
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
        if username and _is_locked(username):
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
            _clear_failures(username)
            login(request, form.get_user())
            return redirect("product_catalogue")
        if username:
            failures = _record_failure(username)
            if failures == LOCKOUT_THRESHOLD:  # notify exactly once per window
                logger.warning("Account lockout triggered")
                _notify_lockout(username)
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
            _send_verification_email(request, request.user)
            messages.success(request, "Verification email sent.")
        except Exception:
            logger.exception("Could not send verification email")
            messages.error(request, "Could not send the email. Try again later.")
    return redirect("profile")
