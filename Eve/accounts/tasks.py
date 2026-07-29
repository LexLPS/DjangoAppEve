"""Background account notifications; task arguments contain identifiers only."""
from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.core import signing
from django.core.mail import send_mail
from django.urls import reverse

EMAIL_TOKEN_SALT = "accounts.email-verification"


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def send_verification_email(self, user_id: int):
    user = User.objects.get(pk=user_id)
    token = signing.dumps({"uid": user.pk}, salt=EMAIL_TOKEN_SALT)
    url = settings.PUBLIC_BASE_URL.rstrip("/") + reverse("verify_email", args=[token])
    send_mail(
        "Verify your Eve email address",
        f"Hi {user.username},\n\n"
        f"Please confirm your email address by opening this link:\n{url}\n\n"
        "The link is valid for 3 days. If you did not create an Eve account, "
        "you can ignore this message.",
        None,
        [user.email],
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
)
def send_lockout_email(self, user_id: int):
    user = User.objects.get(pk=user_id)
    send_mail(
        "Your Eve account was temporarily locked",
        f"Hi {user.username},\n\n"
        "There were repeated failed sign-in attempts on your account, so "
        "sign-in has been paused for about 15 minutes as a precaution.\n\n"
        "If this was you, simply wait and try again. If it was not you, "
        "we recommend resetting your password once sign-in is available "
        "again. Your password has not been changed.",
        None,
        [user.email],
    )
