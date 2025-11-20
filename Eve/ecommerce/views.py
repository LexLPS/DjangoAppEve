from django.shortcuts import render
from .services.saleor_client import fetch_products
from .services.mongo_client import products_collection

def product_catalogue_view(request):
    # Try Mongo cache first
    cached = list(products_collection.find().limit(50))
    if cached:
        products = cached
    else:
        edges = fetch_products()
        products = []
        for edge in edges:
            node = edge["node"]
            products.append(node)
            products_collection.update_one(
                {"id": node["id"]},
                {"$set": node},
                upsert=True,
            )

    return render(request, "ecommerce/product_catalogue.html", {"products": products})