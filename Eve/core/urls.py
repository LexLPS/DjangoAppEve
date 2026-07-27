from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing_view, name="landing"),
    path("contact/", views.contact_view, name="contact"),
    path("healthz/", views.health_view, name="health"),
]