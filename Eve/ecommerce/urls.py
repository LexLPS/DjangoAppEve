from django.urls import path
from . import views

urlpatterns = [
    path("catalogue/", views.product_catalogue_view, name="product_catalogue"),
    path("product/<slug:slug>/", views.product_detail_view, name="product_detail"),

]