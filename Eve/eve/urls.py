"""
URL configuration for eve project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from accounts.api_views import ProfileViewSet
from core.throttling import rate_limit
from django.conf import settings
from django.contrib import admin
from django.urls import include, path
from django_otp.admin import OTPAdminSite
from rest_framework.routers import DefaultRouter

router = DefaultRouter()
router.register(r"profile", ProfileViewSet, basename="profile")

# MFA for administrators: swap in the OTP-enforcing admin site first, then
# wrap its login with brute-force protection (order matters — wrapping first
# would pin the non-OTP login form)
if settings.ADMIN_REQUIRE_MFA:
    admin.site.__class__ = OTPAdminSite
admin.site.login = rate_limit("admin-login", limit=5, window_seconds=300)(admin.site.login)

urlpatterns = [
    # Path is configurable via DJANGO_ADMIN_URL so production doesn't sit on /admin/
    path(settings.ADMIN_URL, admin.site.urls),
    path("accounts/", include("accounts.urls")),
    path("", include("core.urls")),
    path("shop/", include("ecommerce.urls")),
    path("payments/", include("payments.urls")),
    path("api/v1/", include("api.v1.urls")),
    # Legacy unversioned route, kept for existing clients; prefer /api/v1/
    path("api/", include(router.urls)),
    
]

