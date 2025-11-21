import requests
from django.conf import settings

SALEOR_GRAPHQL_URL = settings.SALEOR_GRAPHQL_URL
SALEOR_CHANNEL = getattr(settings, "SALEOR_CHANNEL", "default-channel")


class SaleorAPIError(RuntimeError):
    pass


def fetch_products_from_saleor(first=20):
    if not SALEOR_GRAPHQL_URL:
        raise SaleorAPIError("SALEOR_GRAPHQL_URL is not configured in settings.")

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

    content_type = response.headers.get("content-type", "")

    if "application/json" not in content_type:
        body_preview = response.text[:200]
        raise SaleorAPIError(
            f"Saleor endpoint did not return JSON (content-type={content_type}). "
            f"Body starts with: {body_preview!r}"
        )

    try:
        data = response.json()
    except ValueError:
        raise SaleorAPIError(
            "Saleor GraphQL did not return valid JSON. "
            f"Body starts with: {response.text[:200]!r}"
        )

    if "errors" in data:
        raise SaleorAPIError(f"Saleor GraphQL errors: {data['errors']}")

    edges = data["data"]["products"]["edges"]
    return [edge["node"] for edge in edges]

def fetch_product_by_slug(slug: str):
    if not SALEOR_GRAPHQL_URL:
        raise SaleorAPIError("SALEOR_GRAPHQL_URL is not configured in settings.")

    query = """
    query ($slug: String!, $channel: String!) {
      product(slug: $slug, channel: $channel) {
        id
        name
        slug
        description
        thumbnail {
          url
        }
        media {
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
    """

    variables = {
        "slug": slug,
        "channel": SALEOR_CHANNEL,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    if getattr(settings, "SALEOR_API_TOKEN", None):
        headers["Authorization"] = f"Bearer {settings.SALEOR_API_TOKEN}"

    response = requests.post(
        SALEOR_GRAPHQL_URL,
        json={"query": query, "variables": variables},
        headers=headers,
        timeout=10,
    )

    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        body_preview = response.text[:200]
        raise SaleorAPIError(
            f"Saleor endpoint did not return JSON (content-type={content_type}). "
            f"Body starts with: {body_preview!r}"
        )

    data = response.json()
    if "errors" in data:
        raise SaleorAPIError(f"Saleor GraphQL errors: {data['errors']}")

    product = data["data"]["product"]
    return product  # can be None if slug not found