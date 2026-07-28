from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing_view, name="landing"),
    path("contact/", views.contact_view, name="contact"),
    path("healthz/", views.readiness_view, name="health"),  # legacy alias
    path("healthz/live/", views.liveness_view, name="liveness"),
    path("healthz/ready/", views.readiness_view, name="readiness"),
]