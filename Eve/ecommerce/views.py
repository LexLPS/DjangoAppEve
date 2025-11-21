from django.shortcuts import render, get_object_or_404
from django.http import Http404
from .services.mongo_client import products_collection
from .services.saleor_client import fetch_products_from_saleor, fetch_product_by_slug, SaleorAPIError
import sys


def product_catalogue_view(request):
    products = []
    saleor_error = None

    # 1) Try Mongo cache first
    cached_products = list(products_collection.find().limit(50))
    if cached_products:
        products = cached_products

    # 2) If no cache, try Saleor
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

    # 3) If still nothing AND there was an error, use fallback mock data
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
        # 1) Try Mongo cache first
        cached = products_collection.find_one({"slug": slug})
        if cached:
            product = cached
        else:
            # 2) Fetch from Saleor
            product = fetch_product_by_slug(slug)
            if product is None:
                raise Http404("Product not found")

            # 3) Cache in Mongo
            products_collection.update_one(
                {"id": product["id"]},
                {"$set": product},
                upsert=True,
            )

    except SaleorAPIError as e:
        print("SALEOR ERROR (detail):", e, file=sys.stderr)
        raise Http404("Product not available at the moment")

    return render(request, "ecommerce/product_detail.html", {"product": product})