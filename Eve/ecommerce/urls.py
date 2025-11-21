from django.urls import path
from . import views

urlpatterns = [
    path("catalogue/", views.product_catalogue_view, name="product_catalogue"),
    path("product/<slug:slug>/", views.product_detail_view, name="product_detail"),

    path("cart/", views.cart_view, name="cart"),
    path("cart/add/<slug:slug>/", views.add_to_cart_view, name="add_to_cart"),
    path("cart/remove/<str:product_id>/", views.remove_from_cart_view, name="remove_from_cart"),

]