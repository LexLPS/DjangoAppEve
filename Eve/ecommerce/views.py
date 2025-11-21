from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST
from django.http import Http404
from .services.mongo_client import products_collection
from .services.saleor_client import fetch_products_from_saleor, fetch_product_by_slug, SaleorAPIError
from .services.cart_service import get_cart, add_to_cart, remove_from_cart
import sys


def product_catalogue_view(request):
    products = []
    saleor_error = None

    #  Try Mongo cache first
    cached_products = list(products_collection.find().limit(50))
    if cached_products:
        products = cached_products

    #  If no cache, try Saleor
    if not products:
        try:
            products = fetch_products_from_saleor(first=20)
            print("SALEOR: got", len(products), "products", file=sys.stderr)

            # Save to Mongo only if we actually got something
            if products:
                for product in products:
                    products_collection.update_one(
                        {"id": product["id"]},
                        {"$set": product},
                        upsert=True,
                    )
        except SaleorAPIError as e:
            saleor_error = str(e)
            print("SALEOR ERROR:", e, file=sys.stderr)

    #  If still nothing AND there was an error, use fallback mock data
    if not products and saleor_error is not None:
        products = [
            {
                "id": "1",
                "name": "Eve Horizon – Nature Escape",
                "slug": "eve-horizon-nature-escape",
                "description": "Guided VR walks through forests, beaches, and mountains.",
                "thumbnail": {"url": "https://via.placeholder.com/300x200?text=Nature"},
                "pricing": {
                    "priceRange": {
                        "start": {"gross": {"amount": 49.99, "currency": "EUR"}},
                        "stop": {"gross": {"amount": 49.99, "currency": "EUR"}},
                    }
                },
            },
            {
                "id": "2",
                "name": "Eve Home – Family Moments",
                "slug": "eve-home-family-moments",
                "description": "Recreate familiar home environments for comfort and nostalgia.",
                "thumbnail": {"url": "https://via.placeholder.com/300x200?text=Home"},
                "pricing": {
                    "priceRange": {
                        "start": {"gross": {"amount": 59.99, "currency": "EUR"}},
                        "stop": {"gross": {"amount": 59.99, "currency": "EUR"}},
                    }
                },
            },
        ]

    context = {
        "products": products,
        "saleor_error": saleor_error,
    }
    return render(request, "ecommerce/product_catalogue.html", context)

def product_detail_view(request, slug):
    try:
        #  Try Mongo cache first
        cached = products_collection.find_one({"slug": slug})
        if cached:
            product = cached
        else:
            #  Fetch from Saleor
            product = fetch_product_by_slug(slug)
            if product is None:
                raise Http404("Product not found")

            #  Cache in Mongo
            products_collection.update_one(
                {"id": product["id"]},
                {"$set": product},
                upsert=True,
            )

    except SaleorAPIError as e:
        print("SALEOR ERROR (detail):", e, file=sys.stderr)
        raise Http404("Product not available at the moment")

    return render(request, "ecommerce/product_detail.html", {"product": product})

@login_required
def cart_view(request):
    cart = get_cart(request.user.id)
    items = cart["items"]

    total_amount = 0
    currency = None
    for item in items:
        total_amount += item["price_amount"] * item["quantity"]
        currency = item["price_currency"]  # last one wins; assume same currency

    context = {
        "cart": cart,
        "items": items,
        "total_amount": total_amount,
        "currency": currency,
    }
    return render(request, "ecommerce/cart.html", context)


@login_required
@require_POST
def add_to_cart_view(request, slug):
    quantity = int(request.POST.get("quantity", 1))

    try:
        # try cache
        product = products_collection.find_one({"slug": slug})
        if not product:
            product = fetch_product_by_slug(slug)
            if not product:
                raise Http404("Product not found")

            products_collection.update_one(
                {"id": product["id"]},
                {"$set": product},
                upsert=True,
            )

        add_to_cart(request.user.id, product, quantity)

    except SaleorAPIError as e:
        print("SALEOR ERROR (add_to_cart):", e, file=sys.stderr)
        # silently ignore for now or show message later

    return redirect("cart")


@login_required
@require_POST
def remove_from_cart_view(request, product_id):
    remove_from_cart(request.user.id, product_id)
    return redirect("cart")