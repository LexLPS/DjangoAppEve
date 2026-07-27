import requests
from django.conf import settings

SALEOR_GRAPHQL_URL = settings.SALEOR_GRAPHQL_URL
SALEOR_CHANNEL = settings.SALEOR_CHANNEL


class SaleorAPIError(RuntimeError):
    pass


def saleor_graphql(query: str, variables: dict) -> dict:
    """POST a GraphQL request to Saleor and return the `data` payload.

    Raises SaleorAPIError on transport problems, non-JSON responses, or
    GraphQL-level errors. Error messages may contain response fragments and
    must never be shown to end users — log them server-side only.
    """
    if not SALEOR_GRAPHQL_URL:
        raise SaleorAPIError("SALEOR_GRAPHQL_URL is not configured in settings.")

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if getattr(settings, "SALEOR_API_TOKEN", ""):
        headers["Authorization"] = f"Bearer {settings.SALEOR_API_TOKEN}"

    try:
        response = requests.post(
            SALEOR_GRAPHQL_URL,
            json={"query": query, "variables": variables},
            headers=headers,
            timeout=10,
        )
    except requests.RequestException as exc:
        raise SaleorAPIError(f"Saleor request failed: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "application/json" not in content_type:
        raise SaleorAPIError(
            f"Saleor endpoint did not return JSON (content-type={content_type}). "
            f"Body starts with: {response.text[:200]!r}"
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

    return data["data"]


PRODUCT_FIELDS = """
    id
    name
    slug
    description
    thumbnail {
      url
    }
    defaultVariant {
      id
    }
    pricing {
      priceRange {
        start { gross { amount currency } }
        stop  { gross { amount currency } }
      }
    }
"""


def fetch_products_from_saleor(first=20):
    query = f"""
    query ($first: Int!, $channel: String!) {{
      products(first: $first, channel: $channel) {{
        edges {{
          node {{
            {PRODUCT_FIELDS}
          }}
        }}
      }}
    }}
    """
    data = saleor_graphql(query, {"first": first, "channel": SALEOR_CHANNEL})
    return [edge["node"] for edge in data["products"]["edges"]]


def fetch_product_by_slug(slug: str):
    query = f"""
    query ($slug: String!, $channel: String!) {{
      product(slug: $slug, channel: $channel) {{
        {PRODUCT_FIELDS}
        media {{
          url
        }}
      }}
    }}
    """
    data = saleor_graphql(query, {"slug": slug, "channel": SALEOR_CHANNEL})
    return data["product"]  # can be None if slug not found
