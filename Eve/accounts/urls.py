from core.throttling import rate_limit
from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

urlpatterns = [
    path("register/", views.register_view, name="register"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("profile/", views.profile_view, name="profile"),

    # Email verification
    path("verify-email/<str:token>/", views.verify_email_view, name="verify_email"),
    path("resend-verification/", views.resend_verification_view, name="resend_verification"),

    # Password reset (Django's built-in views; the form POST is rate-limited
    # to stop reset-mail flooding)
    path(
        "password-reset/",
        rate_limit("password-reset", limit=5, window_seconds=3600)(
            auth_views.PasswordResetView.as_view()
        ),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        auth_views.PasswordResetDoneView.as_view(),
        name="password_reset_done",
    ),
    path(
        "reset/<uidb64>/<token>/",
        auth_views.PasswordResetConfirmView.as_view(),
        name="password_reset_confirm",
    ),
    path(
        "reset/done/",
        auth_views.PasswordResetCompleteView.as_view(),
        name="password_reset_complete",
    ),
]
