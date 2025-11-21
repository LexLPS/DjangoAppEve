import requests
from django.conf import settings

SALEOR_GRAPHQL_URL = getattr(settings, "SALEOR_GRAPHQL_URL", "http://localhost:8000/graphql/")
SALEOR_CHANNEL = getattr(settings, "SALEOR_CHANNEL", "default-channel")


def fetch_products_from_saleor(first=20):
    if not SALEOR_GRAPHQL_URL:
        raise ValueError("SALEOR_GRAPHQL_URL is not configured in settings.")

    query = """
    query ($first: Int!, $channel: String!) {
      products(first: $first, channel: $channel) {
        edges {
          node {
            id
            name
            slug
            description
            thumbnail {
              url
            }
            pricing {
              priceRange {
                start { gross { amount currency } }
                stop  { gross { amount currency } }
              }
            }
          }
        }
      }
    }
    """

    variables = {
        "first": first,
        "channel": SALEOR_CHANNEL,
    }

    response = requests.post(
        SALEOR_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        timeout=10,
    )
    response.raise_for_status()

    data = response.json()
    # Basic sanity check
    if "errors" in data:
        raise Exception(f"Saleor GraphQL errors: {data['errors']}")

    edges = data["data"]["products"]["edges"]
    # Flatten edges to product nodes
    products = [edge["node"] for edge in edges]
    return products