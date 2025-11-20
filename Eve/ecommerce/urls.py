from django.urls import path
from . import views

urlpatterns = [
    path("catalogue/", views.product_catalogue_view, name="product_catalogue"),
]